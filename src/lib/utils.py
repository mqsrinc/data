# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import warnings
from pathlib import Path
from functools import partial, reduce
from typing import Any, Callable, List, Dict, Tuple, Optional
from tqdm import tqdm
from pandas import DataFrame, Series, concat, isna, isnull
from .cast import column_convert

ROOT = Path(os.path.dirname(__file__)) / ".." / ".."
CACHE_URL = "https://raw.githubusercontent.com/open-covid-19/data/cache"


def get_or_default(dict_like: Dict, key: Any, default: Any):
    return dict_like[key] if key in dict_like and not isnull(dict_like[key]) else default


def pivot_table(data: DataFrame, pivot_name: str = "pivot") -> DataFrame:
    """ Put a table in our preferred format when the regions are columns and date is index """
    dates = data.index.tolist() * len(data.columns)
    pivots: List[str] = sum([[name] * len(column) for name, column in data.iteritems()], [])
    values: List[Any] = sum([column.tolist() for name, column in data.iteritems()], [])
    records = zip(dates, pivots, values)
    return DataFrame.from_records(records, columns=["date", pivot_name, "value"])


def agg_last_not_null(series: Series, progress_bar: Optional[tqdm] = None) -> Series:
    """ Aggregator function used to keep the last non-null value in a list of rows """
    if progress_bar:
        progress_bar.update()
    return reduce(lambda x, y: y if not isnull(y) else x, series)


def combine_tables(
    tables: List[DataFrame], keys: List[str], progress_label: str = None
) -> DataFrame:
    """ Combine a list of tables, keeping the right-most non-null value for every column """
    data = concat(tables)
    grouped = data.groupby([col for col in keys if col in data.columns])
    if not progress_label:
        return grouped.aggregate(agg_last_not_null).reset_index()
    else:
        progress_bar = tqdm(
            total=len(grouped) * len(data.columns), desc=f"Combine {progress_label} outputs"
        )
        agg_func = partial(agg_last_not_null, progress_bar=progress_bar)
        combined = grouped.aggregate(agg_func).reset_index()
        progress_bar.n = len(grouped) * len(data.columns)
        progress_bar.refresh()
        return combined


def drop_na_records(table: DataFrame, keys: List[str]) -> DataFrame:
    """ Drops all records which have no data outside of the provided keys """
    value_columns = [col for col in table.columns if not col in keys]
    return table.dropna(subset=value_columns, how="all")


def grouped_transform(
    data: DataFrame,
    keys: List[str],
    transform: Callable,
    skip: List[str] = None,
    prefix: Tuple[str, str] = None,
    compat: bool = True,
) -> DataFrame:
    """ Computes the transform for each item within the group determined by `keys` """
    assert keys[-1] == "date", '"date" key should be last'

    # Keep a copy of the columns that will not be transformed
    data = data.sort_values(keys)
    skip = [] if skip is None else skip
    data_skipped = {col: data[col].copy() for col in skip if col in data}

    group = data.groupby(keys[:-1])
    prefix = ("", "") if prefix is None else prefix
    value_columns = [column for column in data.columns if column not in keys + skip]

    data = data.dropna(subset=value_columns, how="all").copy()
    for column in value_columns:
        if column in skip:
            continue
        if data[column].isnull().all():
            continue
        # This behavior can be simplified once all scripts are updated not to perform the
        # grouped transformations on their own
        if compat:
            data[prefix[0] + column] = group[column].apply(transform)
        else:
            data[prefix[0] + column.replace(prefix[1], "")] = group[column].apply(transform)

    # Apply the prefix to all transformed columns
    data = data.rename(columns={col: prefix[1] + col for col in value_columns})

    # Restore the columns that were not transformed
    for name, col in data_skipped.items():
        data[name] = col

    return data


def grouped_diff(
    data: DataFrame,
    keys: List[str],
    skip: List[str] = None,
    prefix: Tuple[str, str] = ("new_", "total_"),
    compat: bool = True,
) -> DataFrame:
    return grouped_transform(
        data, keys, lambda x: x.ffill().diff(), skip=skip, prefix=prefix, compat=compat
    )


def grouped_cumsum(
    data: DataFrame,
    keys: List[str],
    skip: List[str] = None,
    prefix: Tuple[str, str] = ("total_", "new_"),
    compat: bool = True,
) -> DataFrame:
    return grouped_transform(
        data, keys, lambda x: x.fillna(0).cumsum(), skip=skip, prefix=prefix, compat=compat
    )


def stack_table(
    data: DataFrame, index_columns: List[str], value_columns: List[str], stack_columns: List[str]
) -> DataFrame:
    """
    Pivots a DataFrame's columns and aggregates the result as new columns with suffix. E.g.:

    data:

    idx piv val
     0   A   1
     0   B   2
     1   A   3
     1   B   4

    stack_table(data, index_columns=[idx], value_columns=[val], stack_columns=[piv]):

    idx val val_A val_B
     0   3    1     2
     1   7    3     4
    """
    output = data.drop(columns=stack_columns).groupby(index_columns).sum()

    # Stash columns which are not part of the columns being indexed, aggregated or stacked
    used_columns = index_columns + value_columns + stack_columns
    stash_columns = [col for col in data.columns if col not in used_columns]
    stash_output = data[stash_columns].copy()
    data = data.drop(columns=stash_columns)

    # Aggregate (stack) columns with respect to the value columns
    for col_stack in stack_columns:
        col_stack_values = data[col_stack].dropna().unique()
        for col_variable in value_columns:
            df = data[index_columns + [col_variable, col_stack]].copy()
            df = df.pivot_table(
                values=col_variable, index=index_columns, columns=[col_stack], aggfunc="sum"
            )
            column_mapping = {suffix: f"{col_variable}_{suffix}" for suffix in col_stack_values}
            df = df.rename(columns=column_mapping)
            transfer_columns = list(column_mapping.values())
            output[transfer_columns] = df[transfer_columns]

    # Restore the stashed columns, reset index and return
    output[stash_columns] = stash_output
    return output.reset_index()


def age_group(age: int) -> str:
    """
    Categorical age group given a specific age, codified into a function to enforce consistency.
    """
    if age < 15:
        return "children"
    elif age < 65:
        return "adult"
    else:
        return "elderly"


def filter_index_columns(columns: List[str], index_schema: Dict[str, str]) -> List[str]:
    """ Private function used to infer columns that this table should be indexed by """
    index_columns = [col for col in columns if col in index_schema.keys()]
    return index_columns + (["date"] if "date" in columns else [])


def filter_output_columns(columns: List[str], output_schema: Dict[str, str]) -> List[str]:
    """ Private function used to infer columns which are part of the output """
    return [col for col in columns if col in output_schema.keys()]


def infer_new_and_total(data: DataFrame, index_schema: Dict[str, str]) -> DataFrame:
    """
    We use the prefixes "new_" and "total_" as a convention to declare that a column contains values
    which are daily and cumulative, respectively. This helper function will infer daily values when
    only cumulative values are provided (by computing the daily difference) and, conversely, it will
    also infer cumulative values when only daily values are provided (by computing the cumsum).
    """

    index_columns = filter_index_columns(data.columns, index_schema)

    # We only care about columns which have prefix new_ and total_
    prefix_search = ("new_", "total_")
    value_columns = [
        col for col in data.columns if any(col.startswith(prefix) for prefix in prefix_search)
    ]

    # Perform the cumsum of columns which only have new_ values
    tot_columns = [
        col
        for col in data.columns
        if col.startswith("total_") and col.replace("total_", "new_") not in data.columns
    ]
    if tot_columns:
        new_data = grouped_diff(
            data[index_columns + tot_columns], keys=index_columns, compat=False
        ).drop(columns=index_columns)
        data[new_data.columns] = new_data

    # Perform the diff of columns which only have total_ values
    new_columns = [
        col
        for col in data.columns
        if col.startswith("new_") and col.replace("new_", "total_") not in data.columns
    ]
    if new_columns:
        tot_data = grouped_cumsum(
            data[index_columns + new_columns], keys=index_columns, compat=False
        ).drop(columns=index_columns)
        data[tot_data.columns] = tot_data

    return data


def stratify_age_and_sex(data: DataFrame, index_schema: Dict[str, str]) -> DataFrame:
    """
    Some data sources contain age and sex information. The output tables enforce that each record
    must have a unique <key, date> pair (or `key` if no `date` field is present). To solve this
    problem without losing the age and sex information, additional columns are created. For example,
    an input table might have columns [key, date, population, sex] and this function would produce
    the output [key, date, population, population_male, population_female].
    """

    index_columns = filter_index_columns(data, index_schema)
    value_columns = [col for col in data.columns if col not in index_columns]

    # Stack the columns which give us a stratified view of the data
    stack_columns = [col for col in data.columns if col in ("age", "sex")]
    data = stack_table(
        data, index_columns=index_columns, value_columns=value_columns, stack_columns=stack_columns
    )

    return data