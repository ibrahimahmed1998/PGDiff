# PGDiff

A web-based PostgreSQL schema comparison and synchronization tool. PGDiff allows you to compare schemas between two PostgreSQL databases and selectively apply changes from a source database to a target database.

## Features

- **Schema Comparison**: Compare tables, columns, views, functions, and indexes between two PostgreSQL databases
- **Selective Application**: Use checkboxes to select which schema changes you want to apply
- **DDL Generation**: Automatically generates correct SQL DDL statements (CREATE TABLE, ALTER COLUMN, CREATE VIEW, etc.)
- **Safe Execution**: Preview SQL before applying changes with a dry-run mode
- **Per-Item Status**: See success/failure status for each applied change
- **PDF Export**: Export the comparison report as a PDF for documentation and records

## Getting Started

### Prerequisites

- Python 3.7+
- PostgreSQL databases for comparison
- Access credentials for both databases

### Installation

1. Clone or download the repository
2. Install dependencies:
   ```bash
   pip install flask psycopg2-binary
   ```

### Running the Application

1. Start the Flask server:
   ```bash
   python app.py
   ```

2. Open your browser and navigate to:
   ```
   http://localhost:5050
   ```

## Usage

### Connecting Databases

1. Enter the connection details for your **Source Database** (the database with changes you want to copy)
2. Enter the connection details for your **Target Database** (the database that will receive the changes)

### Connection String Format

PostgreSQL connection strings follow this format:

```
postgresql://username:password@hostname:port/database
```

**Example:**
```
postgresql://netwayslmsdbadmin:myPassword@postgresql-lms-server.postgres.database.azure.com:5432/LMSDEV
```

**Components:**
- `username`: Database user account
- `password`: User password
- `hostname`: Server address (localhost, IP address, or fully qualified domain name)
- `port`: PostgreSQL port (default: 5432)
- `database`: Database name

### Comparing Schemas

1. Enter your Source and Target database connection strings
2. Click **Compare** to analyze the differences
3. The tool will display:
   - **Tables**: Only in Source, Only in Target, or Modified columns
   - **Views**: Only in Source or Only in Target
   - **Functions**: Only in Source or Only in Target
   - **Indexes**: Only in Source or Only in Target

### Applying Changes

1. Select the schema changes you want to apply using the **checkboxes**:
   - Check items in "Only in Source" to create them in the target
   - Check items in "Modified" to alter existing objects

2. Click the **Apply to Target** button (appears when items are selected)

3. A modal dialog will show:
   - **SQL Preview**: The exact SQL statements that will be executed
   - **Dry Run Option**: Toggle to preview without applying

4. Click **Confirm** to apply the changes

5. View the results:
   - ✅ Success: Change was applied successfully
   - ❌ Error: Shows what went wrong for troubleshooting

### Exporting to PDF

1. After comparing schemas, click the **Export PDF** button
2. A print dialog will open
3. Choose **Save as PDF** as your destination
4. Name and save the PDF file

The exported PDF contains:
- Comparison date and connection details
- Tables, views, functions, and indexes summary
- Detailed difference listings

## Supported Objects

- **Tables**: Structure comparison and column-level differences
- **Columns**: Data types, constraints, and modifications
- **Views**: Complete view definitions
- **Functions**: Stored procedures and functions
- **Indexes**: Index definitions and properties

## Understanding the Interface

### Source vs. Target Database

- **Source Database**: The reference database with the authoritative schema
- **Target Database**: The database that will be updated to match the source

### Difference Categories

- **Only in Source**: Objects that exist in Source but not in Target (will be created)
- **Only in Target**: Objects that exist in Target but not in Source (for awareness)
- **Modified**: Objects that exist in both but have differences (will be altered)

## Technical Details

### SQL Safety

- All identifiers (table names, column names) are properly quoted using PostgreSQL double-quote syntax
- DDL statements are generated with proper syntax for PostgreSQL
- Transactions ensure atomicity of changes

### Supported DDL Operations

- CREATE TABLE
- CREATE COLUMN
- ALTER COLUMN (type changes)
- CREATE VIEW
- CREATE FUNCTION
- CREATE INDEX

## Troubleshooting

### Connection Issues

- Verify the connection string format is correct
- Ensure network connectivity to both database servers
- Check that credentials are correct and user has necessary permissions
- For Azure PostgreSQL, make sure your connection string includes the full FQDN

### Empty Comparison Results

- Ensure both databases are running and accessible
- Check that the connection strings point to valid databases
- Verify that the user has permissions to query information_schema

### PDF Export

- If export doesn't work, try the browser's native print function (Ctrl+P or Cmd+P)
- Save to PDF from the print dialog
- Ensure your browser allows JavaScript popups

## Security Recommendations

- Do not store credentials in code or config files
- Use environment variables or secure credential management
- Restrict database user permissions to minimum required access
- Test changes on non-production databases first
- Review the SQL preview before applying to production databases
- Keep backups of your target database before applying significant schema changes
