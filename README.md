# Teradata MCP Data Quality Runner

This project demonstrates how to run the **Teradata MCP Server** in **Streamable HTTP mode** and connect to it with a Python client that performs **data quality checks** (nulls, ranges, uniqueness) on your Teradata tables.

---

## 📂 Project Structure

```
.
├── data_quality_client.py   # Python client to run DQ checks via MCP
├── dq_config.yml            # YAML config defining tables and checks
├── .env                     # Environment variables (MCP + DB connection)
├── run_mcp_server.bat       # Windows script to launch MCP server
├── environment.yml          # Conda environment definition
└── README.md                # This file
```

---

## ⚙️ Requirements

- **Teradata database** running and accessible.
- **Python 3.11** (via Conda).
- **Teradata MCP Server** (installed via `pip` or `uv`).

---

## 🚀 Setup

### 1. Clone and enter project
```bash
git clone <this-repo>
cd <this-repo>
```

### 2. Create Conda environment
```bash
conda env create -f environment.yml
conda activate teradata-mcp-dq
```

### 3. Configure `.env`
Copy the template and edit your DB and MCP info:
```bash
cp .env.example .env
```

Inside `.env`:
```env
MCP_ENDPOINT=http://localhost:8001/mcp
DATABASE_URI=teradata://user:password@host:1025/database
```

### 4. Edit `dq_config.yml`
Define which tables and columns to check:
```yaml
datasets:
  - table: sales.orders
    null_check:
      columns: [customer_id, order_date]
    range_check:
      columns:
        - column: amount
          min: 0
          max: 100000
    uniqueness_check:
      columns: [order_id]
```

---

## ▶️ Running

### 1. Start MCP Server

On **Windows**:
```bat
run_mcp_server.bat
```

This will open a new terminal, set Teradata connection variables, and launch:
```
teradata-mcp-server --mcp_transport streamable-http --mcp_port 8001 --profile all
```

### 2. Run Data Quality Client

From your main terminal:
```bash
python data_quality_client.py
```

- Results are logged to the console.  
- Final output is JSON on **stdout** (can be redirected to file).  

Example:
```bash
python data_quality_client.py > dq_results.json
```

---

## 📝 Notes

- The client auto-discovers Data Quality tools from MCP (`tools/list`).
- Only runs **nulls, range, and uniqueness** checks as defined in `dq_config.yml`.
- Run is **one-off**; integrate into schedulers (cron/Airflow) if needed.
- MCP server must be running before you start the client.

---

## 📖 References

- [Teradata MCP Server (GitHub)](https://github.com/Teradata/teradata-mcp-server)  
- [MCP Protocol](https://modelcontextprotocol.io)  
