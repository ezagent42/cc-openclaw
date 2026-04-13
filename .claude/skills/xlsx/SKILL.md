---
name: xlsx
description: Excel spreadsheet creation, editing, and analysis — pandas for data, openpyxl for formulas/formatting, financial modeling standards
user-invocable: true
---

# XLSX creation, editing, and analysis

## Overview

A user may ask you to create, edit, or analyze the contents of an .xlsx file.

## Important Requirements

**LibreOffice Required for Formula Recalculation**: Use `scripts/recalc.py` for recalculating formula values.

## Reading and analyzing data

### Data analysis with pandas
```python
import pandas as pd

df = pd.read_excel('file.xlsx')
all_sheets = pd.read_excel('file.xlsx', sheet_name=None)

df.head()
df.info()
df.describe()

df.to_excel('output.xlsx', index=False)
```

## CRITICAL: Use Formulas, Not Hardcoded Values

Always use Excel formulas instead of calculating values in Python and hardcoding them.

```python
# WRONG
total = df['Sales'].sum()
sheet['B10'] = total

# CORRECT
sheet['B10'] = '=SUM(B2:B9)'
```

## Common Workflow
1. Choose tool: pandas for data, openpyxl for formulas/formatting
2. Create/Load workbook
3. Modify: Add/edit data, formulas, and formatting
4. Save
5. Recalculate formulas (MANDATORY IF USING FORMULAS):
   ```bash
   python scripts/recalc.py output.xlsx
   ```
6. Verify and fix any errors

### Creating new Excel files

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = Workbook()
sheet = wb.active

sheet['A1'] = 'Hello'
sheet['B1'] = 'World'
sheet.append(['Row', 'of', 'data'])

sheet['B2'] = '=SUM(A1:A10)'

sheet['A1'].font = Font(bold=True, color='FF0000')
sheet['A1'].fill = PatternFill('solid', start_color='FFFF00')
sheet['A1'].alignment = Alignment(horizontal='center')

sheet.column_dimensions['A'].width = 20

wb.save('output.xlsx')
```

### Editing existing Excel files

```python
from openpyxl import load_workbook

wb = load_workbook('existing.xlsx')
sheet = wb.active

sheet['A1'] = 'New Value'
sheet.insert_rows(2)
sheet.delete_cols(3)

new_sheet = wb.create_sheet('NewSheet')
new_sheet['A1'] = 'Data'

wb.save('modified.xlsx')
```

## Financial Models

### Color Coding Standards
- **Blue text (0,0,255)**: Hardcoded inputs
- **Black text (0,0,0)**: ALL formulas and calculations
- **Green text (0,128,0)**: Links from other worksheets
- **Red text (255,0,0)**: External links
- **Yellow background (255,255,0)**: Key assumptions

### Number Formatting Standards
- **Years**: Format as text strings ("2024" not "2,024")
- **Currency**: Use $#,##0 format; specify units in headers
- **Zeros**: Use formatting to make zeros "-"
- **Percentages**: Default to 0.0% (one decimal)
- **Multiples**: Format as 0.0x
- **Negative numbers**: Use parentheses (123) not minus -123

### Formula Construction Rules
- Place ALL assumptions in separate assumption cells
- Use cell references instead of hardcoded values
- Document data sources for hardcoded values

## Best Practices

### Library Selection
- **pandas**: Data analysis, bulk operations, simple data export
- **openpyxl**: Complex formatting, formulas, Excel-specific features

### Working with openpyxl
- Cell indices are 1-based
- Use `data_only=True` to read calculated values
- **Warning**: If opened with `data_only=True` and saved, formulas are permanently lost
- Formulas are preserved but not evaluated - use scripts/recalc.py
