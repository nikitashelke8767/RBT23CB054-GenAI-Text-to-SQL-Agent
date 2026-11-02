"""
Text-to-SQL Agent module.
Coordinates LLM-based SQL generation, conservative security validation,
read-only database execution with SQLite authorizer, and deterministic summary generation.
"""

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Load environment variables from .env file if available
load_dotenv()

from database import get_connection, get_schema, resolve_db_path

# Forbidden SQL keywords / operations that must never appear in valid queries
FORBIDDEN_KEYWORDS = [
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "TRUNCATE",
    "EXEC",
    "EXECUTE",
    "VACUUM",
    "REINDEX",
    "GRANT",
    "REVOKE",
    "INTO",
    "UPSERT",
    "TRANSACTION",
    "COMMIT",
    "ROLLBACK",
]


def generate_mock_sql(question: str) -> str:
    """
    Generate deterministic mock SQL queries for offline testing when no API key is provided.
    Explicitly labeled as mock mode to avoid confusing with genuine LLM output.
    """
    q = question.lower()
    if "top" in q and "sales" in q:
        return "SELECT product, SUM(quantity * price) AS total_sales FROM sales GROUP BY product ORDER BY total_sales DESC LIMIT 3;"
    elif "price" in q and ("all" in q or "each" in q or "product" in q):
        return "SELECT product, price FROM sales;"
    elif "total" in q and ("quantity" in q or "units" in q or "items" in q):
        return "SELECT SUM(quantity) AS total_quantity FROM sales;"
    elif "total" in q and ("sales" in q or "revenue" in q):
        return "SELECT SUM(quantity * price) AS total_revenue FROM sales;"
    elif "average" in q or "avg" in q:
        return "SELECT AVG(price) AS average_price FROM sales;"
    elif "highest" in q or "most expensive" in q:
        return "SELECT product, price FROM sales ORDER BY price DESC LIMIT 1;"
    elif "cheapest" in q or "lowest" in q:
        return "SELECT product, price FROM sales ORDER BY price ASC LIMIT 1;"
    else:
        return "SELECT id, product, quantity, price, (quantity * price) AS total_sales FROM sales;"


def generate_sql(
    question: str, schema: str, mock_mode: bool = False
) -> str:
    """
    Generate a SQLite SELECT query from a natural-language question using Gemini API.
    Uses Google GenAI SDK (google-genai).
    """
    if mock_mode:
        return generate_mock_sql(question)

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_api_key_here":
        raise ValueError(
            "Missing Gemini API Key. Please set a valid GEMINI_API_KEY in your .env file or environment variables."
        )

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = f"""You are a strict Text-to-SQL translation assistant. Convert the user's natural language question into a single valid SQLite SELECT query.

Database Schema:
{schema}

Strict Generation Rules:
1. Query ONLY the 'sales' table.
2. Produce ONLY a single read-only SELECT statement.
3. NEVER use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, DETACH, or PRAGMA.
4. Total sales is calculated as (quantity * price).
5. Output ONLY the raw SQL query.
6. Do NOT wrap the query in markdown code blocks (no ``` or ```sql).
7. Do NOT include explanations, conversational filler, or comments.

User Question: {question}
SQL Query:"""

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
            ),
        )

        if not response or not response.text:
            raise ValueError("Gemini API returned an empty response.")

        raw_sql = response.text.strip()

        # Clean markdown code blocks if the LLM wrapped it anyway
        if raw_sql.startswith("```"):
            lines = raw_sql.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_sql = "\n".join(lines).strip()

        return raw_sql

    except Exception as exc:
        raise RuntimeError(f"Gemini API SQL generation failed: {exc}") from exc


def validate_sql(sql: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Validate the generated SQL before execution.
    
    Security rules:
    - Allow only one read-only SELECT statement.
    - Reject empty SQL.
    - Reject multiple statements (semicolon check).
    - Reject INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE, ATTACH, DETACH, PRAGMA, etc.
    - Reject SQL comments (-- , /*, */, #).
    - Restrict queries to the permitted 'sales' table.
    - Reject references to unauthorized tables.
    """
    if not sql or not sql.strip():
        return False, "Validation Error: SQL query cannot be empty."

    trimmed = sql.strip()

    # 1. Reject SQL comments
    if re.search(r"(--|/\*|\*/|#)", trimmed):
        return False, "Validation Error: SQL comments are not permitted."

    # 2. Reject multiple statements
    # Strip optional single trailing semicolon before checking for remaining semicolons
    single_stmt = trimmed.rstrip(";").strip()
    if ";" in single_stmt:
        return False, "Validation Error: Multiple SQL statements are not permitted."

    # 3. Must begin with SELECT
    if not re.match(r"^SELECT\b", single_stmt, re.IGNORECASE):
        return False, "Validation Error: Only read-only SELECT statements are permitted."

    # 4. Reject forbidden modification / administrative keywords
    pattern = r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b"
    forbidden_match = re.search(pattern, single_stmt, re.IGNORECASE)
    if forbidden_match:
        kw = forbidden_match.group(0).upper()
        return (
            False,
            f"Validation Error: Operation '{kw}' is forbidden. Only read-only SELECT queries are allowed.",
        )

    # 5. Restrict to the permitted 'sales' table
    # Strip literal string values inside quotes to prevent false positives in string contents
    no_literals = re.sub(r"'[^']*'", "''", single_stmt)
    no_literals = re.sub(r'"[^"]*"', '""', no_literals)

    # Must contain a FROM clause
    if not re.search(r"\bFROM\b", no_literals, re.IGNORECASE):
        return False, "Validation Error: Query must reference the permitted 'sales' table."

    # Inspect tables in FROM clauses
    from_matches = re.finditer(
        r"\bFROM\s+([^;]+?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|\bWINDOW\b|\bUNION\b|\bJOIN\b|$)",
        no_literals,
        re.IGNORECASE,
    )
    for m in from_matches:
        clause = m.group(1).strip()
        parts = clause.split(",")
        for part in parts:
            part = part.strip()
            if not part or part.startswith("("):
                # Subqueries are allowed if internal tables conform
                continue
            first_token = part.split()[0].strip("\"`'").lower()
            if first_token != "sales":
                return (
                    False,
                    f"Validation Error: Unauthorized table access: '{first_token}'. Only the 'sales' table is permitted.",
                )

    # Inspect tables in JOIN clauses
    join_matches = re.finditer(r"\bJOIN\s+([^\s,;()]+)", no_literals, re.IGNORECASE)
    for m in join_matches:
        tbl = m.group(1).strip("\"`'").lower()
        if tbl != "sales":
            return (
                False,
                f"Validation Error: Unauthorized table access: '{tbl}'. Only the 'sales' table is permitted.",
            )

    return True, None


def execute_sql(
    sql: str, db_path: Optional[str | Path] = None
) -> List[Dict[str, Any]]:
    """
    Execute validated SQL query against the SQLite database.
    
    Security implementation:
    - Opens database connection strictly in read-only mode (mode=ro URI).
    - Sets an SQLite authorizer callback as an additional defensive layer.
    - Prohibits access to any table other than 'sales'.
    - Returns rows converted to a list of standard Python dictionaries.
    """
    resolved_path = resolve_db_path(db_path)
    conn = get_connection(resolved_path, read_only=True)

    # SQLite authorizer callback
    def authorizer_callback(action_code, arg1, arg2, db_name, trigger_name):
        # Allow standard SELECT queries and functions (e.g. SUM, AVG, COUNT, MIN, MAX)
        if action_code in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        # Allow reading columns only from the 'sales' table
        if action_code == sqlite3.SQLITE_READ:
            if arg1 and arg1.lower() != "sales":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        # Deny all other operations (INSERT, UPDATE, DELETE, PRAGMA, ATTACH, etc.)
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer_callback)

    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        result = [dict(row) for row in rows]
        return result
    except Exception as exc:
        raise RuntimeError(f"Database execution error: {exc}") from exc
    finally:
        conn.close()


def create_summary(question: str, results: List[Dict[str, Any]]) -> str:
    """
    Generate a concise, deterministic summary of the query results.
    Avoids extra LLM calls to reduce latency and maintain deterministic behavior.
    """
    if not results:
        return "No records found matching the query."

    num_rows = len(results)
    first_row = results[0]
    keys = list(first_row.keys())

    # Case 1: Single row returned
    if num_rows == 1:
        if len(keys) == 1:
            k = keys[0]
            val = first_row[k]
            val_str = f"{val:,.2f}" if isinstance(val, float) else str(val)
            return f"Query returned 1 record: {k} = {val_str}."
        else:
            items_desc = ", ".join(
                f"{k}: {f'{v:,.2f}' if isinstance(v, float) else v}"
                for k, v in first_row.items()
            )
            return f"Query returned 1 record ({items_desc})."

    # Case 2: Aggregated ranking or top products
    if "product" in keys:
        if "total_sales" in keys:
            top_prod = first_row["product"]
            top_sales = first_row["total_sales"]
            sales_str = f"${top_sales:,.2f}" if isinstance(top_sales, (float, int)) else str(top_sales)
            return f"Retrieved {num_rows} records. Leading product is '{top_prod}' with total sales of {sales_str}."
        elif "price" in keys:
            products = [r["product"] for r in results[:3]]
            sample_str = ", ".join(f"'{p}'" for p in products)
            more = f" and {num_rows - 3} others" if num_rows > 3 else ""
            return f"Retrieved {num_rows} records with product pricing, including {sample_str}{more}."

    # Case 3: General multi-row summary
    return f"Retrieved {num_rows} records with columns: {', '.join(keys)}."


def run_agent(
    question: str,
    db_path: Optional[str | Path] = None,
    mock_mode: bool = False,
) -> Dict[str, Any]:
    """
    Execute the complete Text-to-SQL Agent pipeline:
    1. Accept the user's natural-language question.
    2. Retrieve the database schema.
    3. Generate the SQL query via Gemini (or mock mode if requested).
    4. Validate the query against security rules.
    5. Execute only if validation succeeds.
    6. Generate a deterministic summary.
    7. Return a structured JSON report.
    """
    report: Dict[str, Any] = {
        "question": question,
        "query": None,
        "result": [],
        "summary": None,
        "status": "error",
        "error": None,
    }

    # Step 1 & 2: Schema retrieval
    try:
        schema = get_schema(db_path)
    except Exception as exc:
        report["summary"] = "Failed to retrieve database schema."
        report["error"] = f"Database schema error: {exc}"
        return report

    # Step 3: SQL Generation
    try:
        sql = generate_sql(question, schema, mock_mode=mock_mode)
        report["query"] = sql
    except Exception as exc:
        report["summary"] = "Failed to generate SQL query."
        report["error"] = str(exc)
        return report

    # Step 4: Security Validation
    is_valid, validation_error = validate_sql(sql)
    if not is_valid:
        report["summary"] = "Query execution blocked due to security validation failure."
        report["error"] = validation_error
        return report

    # Step 5: Execution (Only reached if validation succeeds!)
    try:
        results = execute_sql(sql, db_path)
        report["result"] = results
    except Exception as exc:
        report["summary"] = "Query execution failed on the database."
        report["error"] = str(exc)
        return report

    # Step 6: Summary Generation
    summary = create_summary(question, results)
    if mock_mode:
        summary += " [Demonstration Mode]"

    # Step 7: Successful Report
    report["summary"] = summary
    report["status"] = "success"
    report["error"] = None
    return report


if __name__ == "__main__":
    test_q = "Show the top three products by total sales"
    result = run_agent(test_q, mock_mode=True)
    print("Agent Pipeline Output (Mock Mode):")
    print(json.dumps(result, indent=2))
