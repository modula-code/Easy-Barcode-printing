import io
import secrets
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


class PDFSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrintArtifact:
    pdf_bytes: bytes
    png_pages: tuple[bytes, ...]
    page_numbers: tuple[int, ...]
    page_sizes_pt: tuple[tuple[float, float], ...]


class _PrintArtifactCache:
    def __init__(self, max_items: int = 100):
        self.max_items = max_items
        self._items: OrderedDict[str, PrintArtifact] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, artifact: PrintArtifact) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._items[token] = artifact
            self._items.move_to_end(token)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> PrintArtifact | None:
        with self._lock:
            artifact = self._items.get(token)
            if artifact is not None:
                self._items.move_to_end(token)
            return artifact


_artifact_cache = _PrintArtifactCache()
_temp_root = Path(__file__).resolve().parent / "tmp" / "pdfs"


def get_print_artifact(token: str) -> PrintArtifact | None:
    return _artifact_cache.get(token)


def _normalized_search_text(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _count_code_occurrences(page_text: str, code: str) -> int:
    exact_count = page_text.upper().count(code.upper())
    if exact_count:
        return exact_count

    normalized_code = _normalized_search_text(code)
    if len(normalized_code) < 4:
        return 0
    return _normalized_search_text(page_text).count(normalized_code)


def _selected_pages_pdf(reader: PdfReader, page_indexes: list[int]) -> bytes:
    writer = PdfWriter()
    for page_index in page_indexes:
        writer.add_page(reader.pages[page_index])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _render_pages(pdf_bytes: bytes) -> tuple[bytes, ...]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return ()

    _temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=_temp_root) as temporary_directory:
        directory = Path(temporary_directory)
        input_path = directory / "page.pdf"
        output_prefix = directory / "page"
        input_path.write_bytes(pdf_bytes)

        try:
            subprocess.run(
                [
                    pdftoppm,
                    "-png",
                    "-r",
                    "180",
                    str(input_path),
                    str(output_prefix),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PDFSearchError("The matching PDF page could not be rendered.") from exc

        output_paths = sorted(
            directory.glob("page-*.png"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        if not output_paths:
            raise PDFSearchError("The matching PDF page could not be rendered.")
        return tuple(path.read_bytes() for path in output_paths)


def _create_artifact(
    reader: PdfReader,
    page_indexes: list[int],
) -> PrintArtifact:
    selected_pdf = _selected_pages_pdf(reader, page_indexes)
    return PrintArtifact(
        pdf_bytes=selected_pdf,
        png_pages=_render_pages(selected_pdf),
        page_numbers=tuple(page_index + 1 for page_index in page_indexes),
        page_sizes_pt=tuple(
            (
                float(reader.pages[page_index].mediabox.width),
                float(reader.pages[page_index].mediabox.height),
            )
            for page_index in page_indexes
        ),
    )


def prepare_matching_pages(
    pdf_bytes: bytes,
    result_codes: list[str],
) -> dict[str, dict[str, object]]:
    if not pdf_bytes.startswith(b"%PDF-"):
        raise PDFSearchError("The uploaded document is not a valid PDF.")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise PDFSearchError("Password-protected PDFs are not supported.")
    except PDFSearchError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        raise PDFSearchError("The uploaded PDF could not be read.") from exc

    unique_codes = [
        code
        for code in dict.fromkeys(code.strip() for code in result_codes)
        if code
    ]
    page_indexes_by_code: dict[str, list[int]] = {
        code: [] for code in unique_codes
    }
    occurrence_count_by_code = {code: 0 for code in unique_codes}

    for page_index, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""

        for code in unique_codes:
            occurrence_count = _count_code_occurrences(page_text, code)
            if occurrence_count:
                page_indexes_by_code[code].append(page_index)
                occurrence_count_by_code[code] += occurrence_count

    token_by_page_indexes: dict[tuple[int, ...], str] = {}
    results: dict[str, dict[str, object]] = {}
    for code in unique_codes:
        page_indexes = page_indexes_by_code[code]
        if not page_indexes:
            results[code] = {
                "found": False,
                "page_number": None,
                "page_numbers": [],
                "occurrence_count": 0,
                "matching_page_count": 0,
                "first_token": None,
                "all_token": None,
            }
            continue

        first_key = (page_indexes[0],)
        first_token = token_by_page_indexes.get(first_key)
        if first_token is None:
            first_token = _artifact_cache.put(
                _create_artifact(reader, list(first_key))
            )
            token_by_page_indexes[first_key] = first_token

        all_key = tuple(page_indexes)
        all_token = token_by_page_indexes.get(all_key)
        if all_token is None:
            if all_key == first_key:
                all_token = first_token
            else:
                all_token = _artifact_cache.put(
                    _create_artifact(reader, page_indexes)
                )
            token_by_page_indexes[all_key] = all_token

        results[code] = {
            "found": True,
            "page_number": page_indexes[0] + 1,
            "page_numbers": [page_index + 1 for page_index in page_indexes],
            "occurrence_count": occurrence_count_by_code[code],
            "matching_page_count": len(page_indexes),
            "first_token": first_token,
            "all_token": all_token,
        }

    return results
