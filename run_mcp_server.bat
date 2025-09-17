@echo off
REM === Configure Teradata connection ===
set TD_USER==demo_user
set TD_PASSWORD=td-cs$1234
set TD_HOST=td-cs-experience-oe7lr1dbadydeuff.env.clearscape.teradata.com
set TD_NAME=BANK_DB
set TD_PORT=1025

REM === Construct DATABASE_URI for MCP ===
set DATABASE_URI=teradata://demo_user:td-cs$1234@td-cs-experience-oe7lr1dbadydeuff.env.clearscape.teradata.com:1025/BANK_DB

REM === Launch MCP server in new terminal ===
start cmd /k "teradata-mcp-server --mcp_transport streamable-http --mcp_port 8001 --profile all"
