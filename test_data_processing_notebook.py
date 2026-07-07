import ast
import json
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest


NOTEBOOK_PATH = Path(__file__).resolve().parents[1] / "Data_Processing.ipynb"


class _PyodbcStub:
    class Connection:
        pass

    class Cursor:
        pass


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.executemany_calls = []
        self._fetchall_result = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, *params):
        self.executed.append((sql, params))

    def executemany(self, sql, rows):
        self.executemany_calls.append((sql, list(rows)))

    def fetchall(self):
        return self._fetchall_result


class FakeConnection:
    def __init__(self):
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        cur = FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _cell_source(cell_number: int) -> str:
    notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    return "\n".join(notebook["cells"][cell_number - 1]["source"])


def _load_function(cell_number: int, function_name: str):
    source = _cell_source(cell_number)
    tree = ast.parse(source)

    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            target = node
            break

    if target is None:
        raise AssertionError(f"Function '{function_name}' not found in cell {cell_number}")

    module = ast.Module(body=[target], type_ignores=[])
    globals_dict = {
        "pd": pd,
        "pyodbc": _PyodbcStub,
        "Optional": Optional,
        "__builtins__": __builtins__,
    }
    exec(compile(module, filename=str(NOTEBOOK_PATH), mode="exec"), globals_dict)
    return globals_dict[function_name], globals_dict


def test_build_connection_string_uses_trusted_connection_when_no_credentials():
    build_connection_string, _ = _load_function(1, "build_connection_string")

    conn_str = build_connection_string(
        server="LOCALHOST",
        database="sales",
        driver="ODBC Driver 18 for SQL Server",
    )

    assert "SERVER=LOCALHOST" in conn_str
    assert "DATABASE=sales" in conn_str
    assert "Trusted_Connection=yes" in conn_str
    assert "UID=" not in conn_str
    assert "PWD=" not in conn_str


def test_build_connection_string_uses_sql_auth_when_credentials_provided():
    build_connection_string, _ = _load_function(1, "build_connection_string")

    conn_str = build_connection_string(
        server="LOCALHOST",
        database="sales",
        driver="ODBC Driver 18 for SQL Server",
        username="sa",
        password="secret",
    )

    assert "UID=sa" in conn_str
    assert "PWD=secret" in conn_str
    assert "Trusted_Connection=yes" not in conn_str


@pytest.mark.parametrize(
    "kwargs, error_fragment",
    [
        ({"server": "", "database": "sales", "driver": "ODBC Driver"}, "server is required"),
        ({"server": "srv", "database": "", "driver": "ODBC Driver"}, "database is required"),
        ({"server": "srv", "database": "sales", "driver": ""}, "driver is required"),
    ],
)
def test_build_connection_string_validates_required_inputs(kwargs, error_fragment):
    build_connection_string, _ = _load_function(1, "build_connection_string")

    with pytest.raises(ValueError, match=error_fragment):
        build_connection_string(**kwargs)


def test_read_source_file_dispatches_by_type(monkeypatch):
    read_source_file, globals_dict = _load_function(4, "read_source_file")

    expected_xlsx = pd.DataFrame({"a": [1]})
    expected_csv = pd.DataFrame({"b": [2]})
    expected_json = pd.DataFrame({"c": [3]})

    monkeypatch.setattr(globals_dict["pd"], "read_excel", lambda _: expected_xlsx)
    monkeypatch.setattr(globals_dict["pd"], "read_csv", lambda _: expected_csv)
    monkeypatch.setattr(globals_dict["pd"], "read_json", lambda _: expected_json)

    assert read_source_file("xlsx", "x") is expected_xlsx
    assert read_source_file("csv", "x") is expected_csv
    assert read_source_file("json", "x") is expected_json


def test_read_source_file_raises_for_unsupported_type():
    read_source_file, _ = _load_function(4, "read_source_file")

    with pytest.raises(ValueError, match="Unsupported source type"):
        read_source_file("xml", "dummy.xml")


def test_sanitize_columns_normalizes_names():
    sanitize_columns, _ = _load_function(4, "sanitize_columns")
    df = pd.DataFrame(columns=[" Customer Name ", "A-B", "x.y"])

    result = sanitize_columns(df)

    assert list(result.columns) == ["Customer_Name", "A_B", "x_y"]


def test_create_bronze_table_emits_drop_and_create_sql():
    create_bronze_table, _ = _load_function(4, "create_bronze_table")
    cur = FakeCursor()
    df = pd.DataFrame(
        {
            "id": pd.Series([1], dtype="int64"),
            "amount": pd.Series([1.5], dtype="float64"),
            "is_active": pd.Series([True], dtype="bool"),
            "name": pd.Series(["x"], dtype="object"),
        }
    )

    create_bronze_table(cur, "orders", df)

    assert len(cur.executed) == 2
    assert "DROP TABLE bronze.orders" in cur.executed[0][0]
    assert "CREATE TABLE bronze.orders" in cur.executed[1][0]
    assert "[id] BIGINT" in cur.executed[1][0]
    assert "[amount] FLOAT" in cur.executed[1][0]
    assert "[is_active] BIT" in cur.executed[1][0]
    assert "[name] NVARCHAR(MAX)" in cur.executed[1][0]


def test_insert_into_bronze_converts_nan_to_none():
    insert_into_bronze, _ = _load_function(4, "insert_into_bronze")
    cur = FakeCursor()
    df = pd.DataFrame({"id": [1, 2], "profit": [10.5, float("nan")]})

    insert_into_bronze(cur, "orders", df)

    assert len(cur.executemany_calls) == 1
    sql, rows = cur.executemany_calls[0]
    assert "INSERT INTO bronze.orders" in sql
    assert rows == [(1, 10.5), (2, None)]


def test_deduplicate_dataframe_full_row_when_no_keys():
    deduplicate_dataframe, _ = _load_function(5, "deduplicate_dataframe")
    df = pd.DataFrame({"id": [1, 1, 2], "name": ["A", "A", "B"]})

    result = deduplicate_dataframe(df)

    assert len(result) == 2


def test_deduplicate_dataframe_uses_subset_keys_when_present():
    deduplicate_dataframe, _ = _load_function(5, "deduplicate_dataframe")
    df = pd.DataFrame({"id": [1, 1, 1], "name": ["A", "B", "B"]})

    result = deduplicate_dataframe(df, ["id", "name"])

    assert len(result) == 2


def test_deduplicate_dataframe_falls_back_when_keys_missing(capsys):
    deduplicate_dataframe, _ = _load_function(5, "deduplicate_dataframe")
    df = pd.DataFrame({"id": [1, 1], "name": ["A", "A"]})

    result = deduplicate_dataframe(df, ["missing_key"])

    assert len(result) == 1
    assert "Dedup keys ['missing_key'] not found" in capsys.readouterr().out


def test_first_existing_column_in_silver_cell_returns_first_match():
    first_existing_column, _ = _load_function(5, "first_existing_column")
    df = pd.DataFrame(columns=["A", "B", "C"])

    result = first_existing_column(df, ["X", "B", "C"], "test column")

    assert result == "B"


def test_first_existing_column_in_silver_cell_raises_when_missing():
    first_existing_column, _ = _load_function(5, "first_existing_column")
    df = pd.DataFrame(columns=["A"])

    with pytest.raises(ValueError, match="Missing required customer key"):
        first_existing_column(df, ["Customer_ID"], "customer key")


def test_load_bronze_to_silver_commits_on_success():
    load_bronze_to_silver, globals_dict = _load_function(5, "load_bronze_to_silver")

    src_df = pd.DataFrame({"Customer_ID": [1, 1], "name": ["A", "A"]})
    dedup_df = pd.DataFrame({"Customer_ID": [1], "name": ["A"]})
    calls = {"create": 0, "insert": 0}

    globals_dict["read_sql_table"] = lambda connection, schema, table: src_df
    globals_dict["deduplicate_dataframe"] = lambda df, keys: dedup_df

    def _create(cur, schema, table, df):
        calls["create"] += 1
        assert schema == "silver"
        assert table == "customer"
        assert df.equals(dedup_df)

    def _insert(cur, schema, table, df):
        calls["insert"] += 1
        assert schema == "silver"
        assert table == "customer"
        assert df.equals(dedup_df)

    globals_dict["create_table_from_df"] = _create
    globals_dict["insert_dataframe"] = _insert

    conn = FakeConnection()
    load_bronze_to_silver(conn, "customer", ["Customer_ID"])

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert calls["create"] == 1
    assert calls["insert"] == 1


def test_load_bronze_to_silver_rolls_back_and_raises_on_error():
    load_bronze_to_silver, globals_dict = _load_function(5, "load_bronze_to_silver")

    globals_dict["read_sql_table"] = lambda connection, schema, table: (_ for _ in ()).throw(RuntimeError("boom"))
    globals_dict["deduplicate_dataframe"] = lambda df, keys: df
    globals_dict["create_table_from_df"] = lambda cur, schema, table, df: None
    globals_dict["insert_dataframe"] = lambda cur, schema, table, df: None

    conn = FakeConnection()

    with pytest.raises(RuntimeError, match="boom"):
        load_bronze_to_silver(conn, "customer", ["Customer_ID"])

    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_load_enriched_orders_to_silver_builds_enriched_dataframe_and_rounds_profit():
    load_enriched_orders_to_silver, globals_dict = _load_function(5, "load_enriched_orders_to_silver")
    first_existing_column, _ = _load_function(5, "first_existing_column")
    deduplicate_dataframe, _ = _load_function(5, "deduplicate_dataframe")

    orders_df = pd.DataFrame(
        {
            "Order_ID": [1, 1],
            "Customer_ID": [10, 10],
            "Product_ID": [100, 100],
            "Profit": ["10.126", "10.126"],
        }
    )
    customer_df = pd.DataFrame(
        {
            "Customer_ID": [10],
            "Customer_Name": ["Alice"],
            "Country": ["US"],
        }
    )
    products_df = pd.DataFrame(
        {
            "Product_ID": [100],
            "Category": ["Office Supplies"],
            "Sub_Category": ["Paper"],
        }
    )

    def _read_sql_table(connection, schema, table):
        mapping = {
            ("bronze", "orders"): orders_df,
            ("silver", "customer"): customer_df,
            ("silver", "products"): products_df,
        }
        return mapping[(schema, table)]

    captured = {}

    def _create(cur, schema, table, df):
        captured["schema"] = schema
        captured["table"] = table
        captured["df"] = df.copy()

    def _insert(cur, schema, table, df):
        captured["insert_len"] = len(df)

    globals_dict["read_sql_table"] = _read_sql_table
    globals_dict["first_existing_column"] = first_existing_column
    globals_dict["deduplicate_dataframe"] = deduplicate_dataframe
    globals_dict["create_table_from_df"] = _create
    globals_dict["insert_dataframe"] = _insert

    conn = FakeConnection()
    load_enriched_orders_to_silver(conn)

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert captured["schema"] == "silver"
    assert captured["table"] == "orders"
    assert captured["insert_len"] == 1
    assert float(captured["df"]["Profit"].iloc[0]) == pytest.approx(10.13)
    assert "Customer_Name" in captured["df"].columns
    assert "Country" in captured["df"].columns
    assert "Category" in captured["df"].columns
    assert "Sub_Category" in captured["df"].columns


def test_load_enriched_orders_to_silver_rolls_back_and_raises_on_missing_join_keys():
    load_enriched_orders_to_silver, globals_dict = _load_function(5, "load_enriched_orders_to_silver")
    first_existing_column, _ = _load_function(5, "first_existing_column")
    deduplicate_dataframe, _ = _load_function(5, "deduplicate_dataframe")

    orders_df = pd.DataFrame({"Order_ID": [1], "Profit": [1.2], "Product_ID": [10]})
    customer_df = pd.DataFrame({"Customer_ID": [1], "Customer_Name": ["A"], "Country": ["US"]})
    products_df = pd.DataFrame({"Product_ID": [10], "Category": ["Cat"], "Sub_Category": ["Sub"]})

    def _read_sql_table(connection, schema, table):
        mapping = {
            ("bronze", "orders"): orders_df,
            ("silver", "customer"): customer_df,
            ("silver", "products"): products_df,
        }
        return mapping[(schema, table)]

    globals_dict["read_sql_table"] = _read_sql_table
    globals_dict["first_existing_column"] = first_existing_column
    globals_dict["deduplicate_dataframe"] = deduplicate_dataframe
    globals_dict["create_table_from_df"] = lambda cur, schema, table, df: None
    globals_dict["insert_dataframe"] = lambda cur, schema, table, df: None

    conn = FakeConnection()

    with pytest.raises(ValueError, match="orders customer key"):
        load_enriched_orders_to_silver(conn)

    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_first_existing_column_in_gold_cell_signature_and_behavior():
    first_existing_column_gold, _ = _load_function(6, "first_existing_column")
    df = pd.DataFrame(columns=["Order_Date", "Profit"])

    assert first_existing_column_gold(df, ["Date", "Order_Date"]) == "Order_Date"

    with pytest.raises(ValueError, match="None of these columns were found"):
        first_existing_column_gold(df, ["Customer"])
