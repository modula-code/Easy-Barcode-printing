from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, NameObject

reader = PdfReader("/Users/ayena/Documents/barcode-Printing/print-queue-2026-08-03.pdf")
page = reader.pages[0]

content = page.get_contents()
if content:
    stream = ContentStream(content, reader)
    new_operands = []
    
    in_text = False
    for operands, operator in stream.operations:
        op = operator.decode("utf-8") if isinstance(operator, bytes) else operator
        if op == "BT":
            in_text = True
        elif op == "ET":
            in_text = False
        
        # We want to keep everything EXCEPT the data text.
        # But wait, if we skip all text, we lose headers!
        # What if we only skip text within the data table area (Y < 500 and Y > 425) or (Y < 525 and Y > 400 for date/signature)?
        # Actually, if we just use ReportLab from scratch, it's so much easier.
        pass

    # let's just dump it
