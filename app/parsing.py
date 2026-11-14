"""Bounded, page-aware parsers for PDFs, Word, and UTF-8 text, with selective OCR."""
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document as WordDocument
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from app.config import get_settings


class InvalidDocument(ValueError):
    pass


def _validate_docx_zip(path: Path):
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 3000 or sum(f.file_size for f in members) > 80 * 1024 * 1024:
                raise InvalidDocument('DOCX exceeds decompression safety limit')
            for info in members:
                if info.compress_size and info.file_size / info.compress_size > 500:
                    raise InvalidDocument('DOCX contains suspicious compression ratio')
            if 'word/document.xml' not in archive.namelist():
                raise InvalidDocument('Invalid Word document')
    except BadZipFile as exc:
        raise InvalidDocument('Invalid DOCX container') from exc


def _ocr_page(path: Path, number: int) -> str:
    """Render only blank text-layer pages; no executable PDF scripts."""
    import pypdfium2 as pdfium
    import pytesseract
    from PIL import Image
    try:
        pdf = pdfium.PdfDocument(str(path))
        try:
            page = pdf.get_page(number - 1)
            try:
                bitmap = page.render(scale=1.8)
                image = bitmap.to_pil().convert('RGB')
                if image.width * image.height > 14_000_000:
                    raise InvalidDocument('OCR page exceeds image pixel limit')
                return pytesseract.image_to_string(image, config='--psm 3', timeout=45)
            finally:
                page.close()
        finally:
            pdf.close()
    except InvalidDocument:
        raise
    except Exception as exc:
        raise InvalidDocument('OCR failed for a scanned PDF page') from exc


def load_pages(path: Path) -> list[Document]:
    config = get_settings()
    suffix = path.suffix.lower()
    pages: list[Document] = []
    if suffix == '.pdf':
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                raise InvalidDocument('Encrypted PDFs not supported')
            if len(reader.pages) > config.max_pdf_pages:
                raise InvalidDocument('PDF exceeds page limit')
            ocr_count = 0
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ''
                if len(text.strip()) < 10 and config.enable_ocr:
                    ocr_count += 1
                    if ocr_count > config.max_ocr_pages:
                        raise InvalidDocument('Scanned PDF exceeds OCR page limit')
                    text = _ocr_page(path, number)
                if text.strip():
                    pages.append(Document(page_content=text, metadata={'page': number}))
        except InvalidDocument:
            raise
        except Exception as exc:
            raise InvalidDocument('PDF could not be parsed') from exc
    elif suffix == '.docx':
        try:
            _validate_docx_zip(path)
            word = WordDocument(str(path))
            lines = [p.text.strip() for p in word.paragraphs if p.text.strip()]
            for table in word.tables:
                for row in table.rows:
                    line = ' | '.join(cell.text.strip() for cell in row.cells)
                    if line.strip(' |'):
                        lines.append(line)
            pages = [Document(page_content='\n'.join(lines), metadata={'page': None})]
        except InvalidDocument:
            raise
        except Exception as exc:
            raise InvalidDocument('DOCX could not be parsed') from exc
    elif suffix in {'.txt', '.md'}:
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError as exc:
            raise InvalidDocument('Text files must be UTF-8') from exc
        pages = [Document(page_content=text, metadata={'page': None})]
    else:
        raise InvalidDocument('Unsupported document type')
    pages = [page for page in pages if page.page_content.strip()]
    if not pages:
        raise InvalidDocument('No extractable text (enable OCR for scanned PDFs)')
    if sum(len(p.page_content) for p in pages) > config.max_document_chars:
        raise InvalidDocument('Extracted text exceeds character limit')
    return pages


def chunk_pages(pages: list[Document]) -> list[Document]:
    s = get_settings()
    splitter = RecursiveCharacterTextSplitter(chunk_size=s.chunk_size, chunk_overlap=s.chunk_overlap,
                                              separators=['\n\n', '\n', '. ', ' ', ''])
    chunks = splitter.split_documents(pages)
    if not chunks or len(chunks) > s.max_chunks:
        raise InvalidDocument('Document has too many or too few chunks')
    for index, chunk in enumerate(chunks):
        chunk.metadata['chunk_index'] = index
    return chunks
