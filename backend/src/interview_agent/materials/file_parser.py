"""Bounded text-only parsing. No OCR, macros, URLs or document instructions are executed."""
import hashlib
import io
import json
import sys
import unicodedata
import zipfile
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT = 20000
MAX_XML = 2 * 1024 * 1024


class ParseError(ValueError):
    pass


class DocumentTree(ET.TreeBuilder):
    def doctype(self,*args):
        raise ParseError('material_docx_unsupported_content')


def parse_document(data, kind):
    if not data or len(data) > MAX_FILE_BYTES:
        raise ParseError('material_file_size_limit')
    blocks = []
    warnings = ['请核对解析顺序、遗漏和文字后再导入；解析文本不等于已确认事实。']
    if kind == 'pdf':
        from pypdf import PdfReader, __version__, overwrite_configuration
        overwrite_configuration(maximum_declared_stream_length=MAX_XML,
            array_based_stream_maximum_output_length=MAX_XML,zlib_maximum_output_length=MAX_XML,
            lzw_maximum_output_length=MAX_XML,run_length_maximum_output_length=MAX_XML,
            brotli_maximum_output_length=MAX_XML,stream_decoding_work_maximum_length=8*MAX_XML,
            stream_parsing_work_maximum_count=100000,page_tree_maximum_entries=1000)
        if not data.startswith(b'%PDF-'):
            raise ParseError('material_file_type_mismatch')
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ParseError('material_pdf_encrypted')
        if not 1 <= len(reader.pages) <= 10:
            raise ParseError('material_pdf_page_limit')
        for number, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_XML:
                raise ParseError('material_pdf_content_limit')
            text = page.extract_text() or ''
            if not text.strip():
                warnings.append(f'第{number}页没有可提取文字，可能是图片/空白页；本系统未进行OCR。')
            blocks.append(('page', number, text))
        parser = 'pypdf/' + __version__
        warnings.append('PDF阅读顺序、分栏、表格和嵌入图片不保证完整还原。')
    elif kind == 'docx':
        if not data.startswith(b'PK'):
            raise ParseError('material_file_type_mismatch')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 200 or len({i.filename for i in infos}) != len(infos):
                raise ParseError('material_docx_archive_limit')
            if sum(i.file_size for i in infos) > 8 * MAX_XML:
                raise ParseError('material_docx_archive_limit')
            if any(i.flag_bits & 1 for i in infos) or any('vbaproject' in i.filename.lower() for i in infos):
                raise ParseError('material_docx_unsupported_content')
            part = archive.getinfo('word/document.xml')
            if part.file_size > MAX_XML:
                raise ParseError('material_docx_xml_limit')
            xml = archive.read(part)
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ParseError('material_docx_unsupported_content')
        root = ET.fromstring(xml,parser=ET.XMLParser(target=DocumentTree()))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        body = root.find(ns + 'body')
        if body is None:
            raise ParseError('material_file_invalid')
        for number, paragraph in enumerate(body.iter(ns + 'p'), 1):
            if number > 1000:
                raise ParseError('material_docx_paragraph_limit')
            text = ''.join((node.text or '') if node.tag == ns + 't' else
                           '\t' if node.tag == ns + 'tab' else '\n' if node.tag in (ns + 'br', ns + 'cr') else ''
                           for node in paragraph.iter())
            blocks.append(('paragraph', number, text))
        parser = 'stdlib-wordprocessingml/v1'
        warnings.append('仅解析DOCX正文（含表格内段落）；页眉、页脚、图片、批注和修订语义未还原，段落号不是页码。')
    else:
        raise ParseError('unsupported_material_format')
    parts, locations, size = [], [], 0
    for unit, number, text in blocks:
        text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
        if not text:
            continue
        if '\ufffd' in text or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ParseError('material_file_text_unreadable')
        start = size + (2 if parts else 0)
        size = start + len(text)
        if size > MAX_TEXT:
            raise ParseError('material_file_text_limit')
        parts.append(text)
        locations.append({'unit': unit, 'number': number, 'start_char': start, 'end_char': size})
    if not parts:
        raise ParseError('material_file_no_text')
    # F07Z-98: PDF text may contain compatibility ideographs (Kangxi radicals) that break
    # heading detection and read badly; map only those, leaving punctuation as authored.
    from .segments import normalize_text
    text = normalize_text('\n\n'.join(parts))
    return {'text': text, 'content_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'metadata': {'parser': parser, 'text_policy': 'normalized-blocks-v1', 'locations': locations, 'warnings': warnings}}


def main():
    try:
        result = {'ok': True, 'parsed': parse_document(sys.stdin.buffer.read(MAX_FILE_BYTES + 1), sys.argv[1])}
    except Exception as error:
        result = {'ok': False, 'code': str(error) if isinstance(error,ParseError) else 'material_file_invalid'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
