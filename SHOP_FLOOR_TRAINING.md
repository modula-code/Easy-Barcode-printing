# Shop Floor Training: Label Printing

## Purpose

Use this app to find and print the correct label page for a PO and SM code.

## Before You Start

- Keep the barcode scanner connected.
- Keep the printer ready.
- Keep the label PDF ready, or fetch it from Odoo.
- Ask your supervisor if the PO number or SM code is not clear.

## Screen Fields

- **PO number**: Enter or scan the purchase order number.
- **SM code 1**: Scan the first SM code.
- **SM code 2**: Scan the second SM code if needed.
- **Label PDF**: Fetch the PDF from Odoo or upload a PDF file.

## Daily Steps

1. Open the **Label Printing** screen.
2. Enter or scan the **PO number**.
3. Press **Check PO** if you want to confirm the linked SO number.
4. Press **Fetch Label PDF**.
5. Wait until the screen says **Label PDF fetched and locked.**
6. Scan **SM code 1**.
7. Scan **SM code 2** if the job needs two codes.
8. Press **Find label pages**.
9. Check the result.
10. Press **Print** to print the matching label page.
11. If the screen shows **Print all**, press it only when all matching label pages are needed.

## If You Upload a PDF Manually

1. Select the PDF file in **Label PDF**.
2. Make sure the correct PO number is entered.
3. Scan the SM code or codes.
4. Press **Find label pages**.
5. Print the matching label page.

## Important Rules

- Use the correct PO number before fetching the label PDF.
- If the PO number changes, fetch the label PDF again.
- Scan at least one SM code.
- Use two SM codes only when the job needs both codes to match.
- Do not print if the part code or label page looks wrong.
- Do not refresh or close the page while printing is in progress.

## Common Messages

| Message | What It Means | What To Do |
| --- | --- | --- |
| **Enter a PO number first.** | The PO number is missing. | Enter or scan the PO number. |
| **Label PDF fetched and locked.** | The PDF is ready. | Continue with SM code scanning. |
| **PO changed. Fetch the label PDF again.** | The PO was changed after fetching the PDF. | Press **Fetch Label PDF** again. |
| **Using uploaded PDF.** | A manual PDF was selected. | Continue if this is the correct PDF. |
| **Enter at least one product code.** | No SM code was entered. | Scan SM code 1. |
| **No panel matches both SM codes.** | The two SM codes do not point to the same panel. | Check both codes and scan again. |
| **No matching panel found.** | No panel was found for the code. | Check the PO and SM code. |
| **Part code was not found in the uploaded PDF.** | The app found the part, but not in the PDF. | Check that the PDF is correct. |

## Good Practice

- Scan slowly enough to confirm each field is filled.
- Check the page count before pressing **Print all**.
- Keep printed labels with the correct job material.
- Report repeated lookup or print errors to the supervisor.

## Quick Checklist

- Correct PO number entered.
- Label PDF fetched or uploaded.
- SM code scanned.
- Result checked.
- Correct label printed.
