"""
Database module for the Text-to-SQL Agent.
Handles SQLite database initialization, sample data seeding, and schema retrieval.
"""

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

# Default database file path in the same directory as this file
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sales.db"

# Sample sales records: at least 5 records with multiple products
SAMPLE_SALES_DATA = [
    ("Wireless Mouse", 15, 25.50),
    ("Mechanical Keyboard", 8, 89.99),
    ("USB-C Hub", 20, 34.00),
    ("27-inch Monitor", 5, 249.99),
    ("Noise-Cancelling Headphones", 12, 129.50),
    ("Webcam 1080p", 10, 49.99),
    ("Laptop Stand", 25, 29.99),
    ("Wireless Mouse", 10, 25.50),
]


def resolve_db_path(db_path: Optional[str | Path] = None) -> Path:
    """Resolve and return the absolute database Path."""
    if db_path is None:
        return DEFAULT_DB_PATH
    return Path(db_path).resolve()


def get_connection(
    db_path: Optional[str | Path] = None, read_only: bool = False
) -> sqlite3.Connection:
    """
    Get a database connection.
    If read_only is True, opens using SQLite URI with mode=ro.
    """
    resolved_path = resolve_db_path(db_path)
    if read_only:
        if not resolved_path.exists():
            # If the database does not exist yet, initialize it first
            init_db(resolved_path)
        uri = f"file:{resolved_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(resolved_path)

    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str | Path] = None) -> int:
    """
    Initialize the SQLite database.
    Creates the sales table and inserts sample data only if the table is empty.
    Returns the total number of records currently in the sales table.
    """
    resolved_path = resolve_db_path(db_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(resolved_path)
    cursor = conn.cursor()

    # 1. Create sales table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        );
        """
    )

    # 2. Check if table is empty
    cursor.execute("SELECT COUNT(*) FROM sales;")
    count = cursor.fetchone()[0]

    # 3. Seed sample data only when table is empty
    if count == 0:
        cursor.executemany(
            """
            INSERT INTO sales (product, quantity, price)
            VALUES (?, ?, ?);
            """,
            SAMPLE_SALES_DATA,
        )
        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM sales;")
        count = cursor.fetchone()[0]

    conn.close()
    return count


def get_schema(db_path: Optional[str | Path] = None) -> str:
    """
    Retrieve and display the table schema and column information.
    Formatted cleanly for the LLM prompt and UI inspection.
    """
    resolved_path = resolve_db_path(db_path)
    if not resolved_path.exists():
        init_db(resolved_path)

    conn = get_connection(resolved_path, read_only=True)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(sales);")
    columns = cursor.fetchall()
    conn.close()

    col_descriptions = []
    for col in columns:
        col_name = col["name"]
        col_type = col["type"]
        is_pk = " (PRIMARY KEY)" if col["pk"] else ""
        col_descriptions.append(f"  - {col_name}: {col_type}{is_pk}")

    schema_str = (
        "Table: sales\n"
        "Columns:\n"
        + "\n".join(col_descriptions)
        + "\nCalculation rule:\n"
        "  - Total sales is calculated as (quantity * price)"
    )
    return schema_str


def get_all_sales(db_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """
    Retrieve all records from the sales table.
    Used for database preview in Streamlit UI.
    """
    resolved_path = resolve_db_path(db_path)
    if not resolved_path.exists():
        init_db(resolved_path)

    conn = get_connection(resolved_path, read_only=True)
    cursor = conn.cursor()
    cursor.execute("SELECT id, product, quantity, price, (quantity * price) AS total_sales FROM sales ORDER BY id ASC;")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


if __name__ == "__main__":
    count = init_db()
    print(f"Database initialized with {count} records at {DEFAULT_DB_PATH}")
    print("\nDatabase Schema:")
    print(get_schema())
