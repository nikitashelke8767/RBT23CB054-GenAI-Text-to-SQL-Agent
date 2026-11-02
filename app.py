"""
Streamlit Web Application for the GenAI Text-to-SQL Agent.
Provides an interactive interface to query the SQLite sales database using natural language.
"""

import json
import os
from pathlib import Path
import streamlit as st
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import agent and database modules
import agent
import database

# Initialize the SQLite database automatically on application start
database.init_db()

# Page configuration
st.set_page_config(
    page_title="GenAI Text-to-SQL Agent",
    page_icon="🔍",
    layout="wide",
)

# Application Title and Description
st.title("GenAI Text-to-SQL Agent")
st.markdown(
    """
    This application translates natural-language questions into safe, validated SQLite queries,
    executes them against the local sales database in read-only mode, and produces a structured JSON report.
    """
)

# Check API Key status
api_key = os.getenv("GEMINI_API_KEY", "").strip()
has_api_key = bool(api_key and api_key != "your_api_key_here")

# Sidebar: Database Preview and Settings
with st.sidebar:
    st.header("⚙️ Configuration")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    st.text(f"Model: {model_name}")

    if not has_api_key:
        st.warning("⚠️ No Gemini API key detected in `.env`.")
        mock_mode_default = True
    else:
        st.success("✅ Gemini API Key detected.")
        mock_mode_default = False

    mock_mode = st.checkbox(
        "Enable Mock Mode (Offline Testing)",
        value=mock_mode_default,
        help="Simulates LLM generation locally without calling the Gemini API.",
    )

    if mock_mode:
        st.info("ℹ️ Running in Mock Demonstration Mode.")

    st.divider()

    st.header("📊 Database Preview (`sales`)")
    try:
        sales_records = database.get_all_sales()
        st.dataframe(sales_records, use_container_width=True)
    except Exception as e:
        st.error(f"Failed to load database preview: {e}")

    with st.expander("View Table Schema"):
        st.code(database.get_schema(), language="sql")

# Example Questions Section
st.subheader("💡 Example Questions")
examples = [
    "Show all products and their prices.",
    "Show the top three products by total sales.",
    "What is the total quantity of items sold?",
    "What is the total revenue generated from all sales?",
    "Show products ordered from highest to lowest price.",
]

cols = st.columns(len(examples))
for i, ex in enumerate(examples):
    if cols[i].button(ex, key=f"ex_{i}"):
        st.session_state["question_input"] = ex

# Question Input Area
default_question = st.session_state.get(
    "question_input", "Show the top three products by total sales."
)
user_question = st.text_input(
    "Enter your natural-language question:",
    value=default_question,
    placeholder="e.g. Show all products with their prices",
)

# Unsafe Test queries helper for testing
with st.expander("Security Test Presets (Verify Query Validation)"):
    st.caption("Click any preset below to test conservative security rejection:")
    test_cols = st.columns(3)
    if test_cols[0].button("⚠️ Test DELETE Statement"):
        st.session_state["question_input"] = "DELETE FROM sales"
        st.rerun()
    if test_cols[1].button("⚠️ Test Unauthorized Table"):
        st.session_state["question_input"] = "SELECT * FROM users"
        st.rerun()
    if test_cols[2].button("⚠️ Test SQL Comment Injection"):
        st.session_state["question_input"] = "SELECT * FROM sales -- drop table"
        st.rerun()

# Execute Action
generate_btn = st.button("Generate and Execute SQL", type="primary")

if generate_btn:
    if not user_question.strip():
        st.warning("Please enter a question or query before proceeding.")
    else:
        # Check if the user is testing raw SQL directly in the input box
        # If it looks like raw SQL (e.g., DELETE, DROP, SELECT), test validation directly
        raw_upper = user_question.strip().upper()
        if raw_upper.startswith(("DELETE", "DROP", "INSERT", "UPDATE", "ALTER", "CREATE", "SELECT")):
            # Direct validation test
            query = user_question.strip()
            is_valid, val_err = agent.validate_sql(query)
            if not is_valid:
                report = {
                    "question": user_question,
                    "query": query,
                    "result": [],
                    "summary": "Query execution blocked due to security validation failure.",
                    "status": "error",
                    "error": val_err,
                }
            else:
                try:
                    res = agent.execute_sql(query)
                    report = {
                        "question": user_question,
                        "query": query,
                        "result": res,
                        "summary": agent.create_summary(user_question, res),
                        "status": "success",
                        "error": None,
                    }
                except Exception as exc:
                    report = {
                        "question": user_question,
                        "query": query,
                        "result": [],
                        "summary": "Query execution failed on the database.",
                        "status": "error",
                        "error": str(exc),
                    }
        else:
            with st.spinner("Processing question with Text-to-SQL Agent..."):
                report = agent.run_agent(
                    question=user_question,
                    mock_mode=mock_mode,
                )

        st.session_state["latest_report"] = report

# Render Report Output if present
if "latest_report" in st.session_state:
    report = st.session_state["latest_report"]

    st.divider()

    # 1. Validation Status & Error Handling
    if report["status"] == "success":
        st.success("✅ **Validation Status: PASSED** (Read-only SELECT query verified safe)")
    else:
        st.error(f"❌ **Validation / Execution Status: FAILED**\n\n{report['error']}")

    # 2. Generated SQL Query
    st.subheader("Generated SQL Query")
    if report["query"]:
        st.code(report["query"], language="sql")
    else:
        st.write("*(No query generated)*")

    # 3. Query Results Table
    st.subheader("Query Results")
    if report["status"] == "success" and report["result"]:
        st.dataframe(report["result"], use_container_width=True)
    elif report["status"] == "success":
        st.info("Query executed successfully, but returned 0 rows.")
    else:
        st.write("*(No results to display due to query rejection or error)*")

    # 4. Summary
    st.subheader("Summary")
    if report["status"] == "success":
        st.info(f"💡 {report['summary']}")
    else:
        st.warning(f"⚠️ {report['summary']}")

    # 5. Complete Structured JSON Report
    with st.expander("Complete Structured JSON Report", expanded=True):
        st.json(report)
