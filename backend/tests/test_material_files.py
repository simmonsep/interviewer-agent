import io
import subprocess
import zipfile
from uuid import uuid4
from xml.sax.saxutils import escape
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject,ArrayObject,TextStringObject
from interview_agent.materials.file_parser import parse_document,MAX_FILE_BYTES
from interview_agent.materials.file_repository import parse_in_child
from interview_agent.storage.errors import ApiError
from test_material_repository import material_context,worker_settings,pg_settings,post,url,view,analyze,confirm
from test_interview_repository import create,process as interview_process,view as interview_view
from test_postgres import arun


def docx(*paragraphs):
    xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    xml+=''.join('<w:p><w:r><w:t>'+escape(p)+'</w:t></w:r></w:p>' for p in paragraphs)+'</w:body></w:document>'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('word/document.xml',xml)
    return out.getvalue()


def cjk_pdf():
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    cid=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/CIDFontType0'),
        NameObject('/BaseFont'):NameObject('/STSong-Light'),NameObject('/CIDSystemInfo'):DictionaryObject({
            NameObject('/Registry'):TextStringObject('Adobe'),NameObject('/Ordering'):TextStringObject('GB1')})})
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type0'),
        NameObject('/BaseFont'):NameObject('/STSong-Light'),NameObject('/Encoding'):NameObject('/UniGB-UCS2-H'),
        NameObject('/DescendantFonts'):ArrayObject([writer._add_object(cid)])})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 40 720 Td <'+'我负责SQL事务。'.encode('utf-16-be').hex()+'> Tj ET').encode())
    page[NameObject('/Contents')]=writer._add_object(stream)
    out=io.BytesIO();writer.write(out);return out.getvalue()


def test_cjk_pdf_and_docx_oversized_xml():
    assert parse_in_child(cjk_pdf(),'pdf')['text']=='我负责SQL事务。'
    with pytest.raises(ValueError,match='material_docx_xml_limit'):
        parse_document(docx('x'*(2*1024*1024)),'docx')


def test_utf16_docx_doctype_and_pdf_compressed_stream_are_rejected():
    out=io.BytesIO()
    xml='<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE document [<!ENTITY sample "expanded">]><document>&sample;</document>'
    with zipfile.ZipFile(out,'w') as z:z.writestr('word/document.xml',xml.encode('utf-16'))
    with pytest.raises(ValueError,match='material_docx_unsupported_content'):parse_document(out.getvalue(),'docx')
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    stream=DecodedStreamObject();stream.set_data(b' '*(2*1024*1024+1))
    page[NameObject('/Contents')]=writer._add_object(stream.flate_encode())
    out=io.BytesIO();writer.write(out)
    with pytest.raises(ApiError) as error:parse_in_child(out.getvalue(),'pdf')
    assert error.value.code in ('material_file_invalid','material_pdf_content_limit')


def pdf(*pages,encrypted=False):
    writer=PdfWriter()
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    for text in pages:
        page=writer.add_blank_page(width=612,height=792)
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 40 720 Td ('+text+') Tj ET').encode('ascii'))
        page[NameObject('/Contents')]=writer._add_object(stream)
    if encrypted:writer.encrypt('Synthetic-not-a-real-password')
    out=io.BytesIO();writer.write(out);return out.getvalue()


def test_pdf_pages_and_docx_chinese_keep_exact_location_offsets():
    for data,kind in [(pdf('SQL transactions','Redis notifications'),'pdf'),(docx('我负责SQL事务。','验证Redis通知。'),'docx')]:
        result=parse_document(data,kind)
        assert len(result['metadata']['locations'])==2
        for span,expected in zip(result['metadata']['locations'],['SQL transactions','Redis notifications'] if kind=='pdf' else ['我负责SQL事务。','验证Redis通知。']):
            assert result['text'][span['start_char']:span['end_char']]==expected
        assert parse_in_child(data,kind)==result


@pytest.mark.parametrize('data,kind,code',[
    (pdf('protected',encrypted=True),'pdf','material_pdf_encrypted'),
    (pdf(''),'pdf','material_file_no_text'),
    (pdf(*['x']*11),'pdf','material_pdf_page_limit'),
    (docx('x'*20001),'docx','material_file_text_limit'),
    (b'x'*(MAX_FILE_BYTES+1),'pdf','material_file_size_limit'),
    (b'not PDF','pdf','material_file_type_mismatch'),
],ids=['encrypted','empty','page-limit','text-limit','file-limit','wrong-type'])
def test_unsupported_inputs_fail_without_silent_truncation(data,kind,code):
    with pytest.raises(ValueError,match=code):parse_document(data,kind)


def test_invalid_docx_and_child_timeout_are_stable_codes(monkeypatch):
    with pytest.raises(ApiError) as error:parse_in_child(b'PK-invalid','docx')
    assert error.value.code=='material_file_invalid'
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('synthetic',8)
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(ApiError) as error:parse_in_child(b'ignored','pdf')
    assert error.value.code=='material_file_parse_timeout'


def preview(c,g,data=None,name='简历.docx',key=None,revision=0):
    return c.post(url(g)+'/file-previews',params={'filename':name,'expected_version':1,'expected_revision':revision},
        content=data if data is not None else docx('我负责SQL事务和Redis通知。','通过独立数据库验证取消后旧结果不能提交。'),
        headers={'Idempotency-Key':key or uuid4().hex,'Content-Type':'application/octet-stream'})


def import_preview(c,g,p,key=None,revision=0):
    return post(c,url(g)+f"/file-previews/{p['preview_id']}/import",{'expected_version':1,'expected_revision':revision},key)


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.parametrize('kind',['docx','pdf'])
def test_owned_preview_import_analysis_facts_and_interview(material_context,worker_settings,kind):
    c,g=material_context
    data=docx('我负责SQL事务和Redis通知。','通过独立数据库验证取消后旧结果不能提交。') if kind=='docx' else pdf('Implemented SQL transactions.','Verified Redis delivery.')
    key=uuid4().hex;p=preview(c,g,data,'resume.'+kind,key)
    assert p.status_code==201,p.text
    p=p.json();assert preview(c,g,data,'resume.'+kind,key).json()['preview_id']==p['preview_id']
    assert view(c,g)['sources']==[] and view(c,g)['material_revision']==0
    assert c.get(url(g)+'/file-previews/'+p['preview_id']).json()['text']==p['text']
    ikey=uuid4().hex;s=import_preview(c,g,p,ikey)
    assert s.status_code==201,s.text
    assert import_preview(c,g,{**p,'preview_id':p['preview_id'].upper()},ikey).status_code==200
    assert view(c,g)['material_revision']==1
    r=analyze(c,g,worker_settings);assert not view(c,g)['facts']
    assert confirm(c,g,r).status_code==201
    v=view(c,g);facts=v['facts'];assert len(facts)==2
    assert all(f['file_locations'] for f in facts)
    assert v['sources'][0]['file_reference']['file_sha256']==p['file_sha256']
    assert all(p['text'][f['start_char']:f['end_char']]==f['quote'] for f in facts)
    session=create(c,g,fact_ids=[f['id'] for f in facts])
    assert session.status_code==202,session.text
    arun(interview_process(worker_settings))
    saved=interview_view(c,g,session.json())
    assert saved['status']=='WAITING_FOR_USER'
    question=next(e for e in saved['events'] if e['kind']=='question')
    assert question['payload']['topic_id']=='project'


@pytest.mark.postgres
@pytest.mark.redis
def test_stale_preview_conflict_and_foreign_goal_are_rejected(material_context,pg_settings):
    c,g=material_context;k=uuid4().hex;p=preview(c,g,key=k).json()
    assert preview(c,g,data=docx('changed'),key=k).status_code==409
    other=post(c,'/api/career-goals/',{'message':'另一个目标'}).json()
    assert c.get(url(other)+'/file-previews/'+p['preview_id']).status_code==404
    assert import_preview(c,other,p).status_code==404
    from test_postgres import client_for,signup,query
    with client_for(pg_settings[0]) as foreign:
        foreign.headers['Origin']=pg_settings[0].origin
        uid=signup(foreign)['user']['id']
        assert foreign.get(url(g)+'/file-previews/'+p['preview_id']).status_code==404
        assert preview(foreign,g).status_code==404
        assert import_preview(foreign,g,p).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))
    from test_material_repository import source
    assert source(c,g).status_code==201
    assert c.get(url(g)+'/file-previews/'+p['preview_id']).json()['stale']
    assert import_preview(c,g,p,revision=1).json()['code']=='material_file_preview_stale'


@pytest.mark.postgres
@pytest.mark.redis
def test_preview_body_limit_origin_auth_and_invalid_file_do_not_create_sources(material_context):
    c,g=material_context
    assert preview(c,g,b'x'*(MAX_FILE_BYTES+1)).status_code==413
    assert preview(c,g,b'%PDF-broken','resume.pdf').status_code==400
    assert preview(c,g,docx('x'),'../resume.docx').status_code==400
    c.headers['Origin']='http://untrusted.invalid'
    assert preview(c,g).status_code==403
    c.headers['Origin']=str(c.base_url).rstrip('/')
    assert view(c,g)['sources']==[]
    c.cookies.clear();assert preview(c,g).status_code==401


def test_extracted_text_normalises_compatibility_ideographs():
    parsed=parse_document(docx('⼯作经历','2018.10 某公司'),'docx')
    assert '工作经历' in parsed['text'] and '⼯' not in parsed['text']
