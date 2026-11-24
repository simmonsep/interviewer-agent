"""Daily resume commands over the existing authenticated local API."""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from uuid import UUID,uuid4
import httpx

ORIGIN='http://127.0.0.1:3100'
COOKIE='py_interview_session'


class ResumeClient:
    def __init__(self,client,state):
        self.client=client;self.state=Path(state).resolve()
        if '.private' not in self.state.parts:raise ValueError('session_state_requires_private_directory')
        self.saved=json.loads(self.state.read_text(encoding='utf-8')) if self.state.exists() else {}
        if self.saved.get('origin',ORIGIN)!=ORIGIN:raise ValueError('session_origin_mismatch')
        client.headers['Origin']=ORIGIN
        if self.saved.get('token'):client.cookies.set(COOKIE,self.saved['token'],domain='127.0.0.1',path='/')

    def save(self):
        self.saved['origin']=ORIGIN
        self.saved['token']=self.client.cookies.get(COOKIE)
        self.state.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(self.state,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(self.saved,f,ensure_ascii=False)

    def request(self,path,body=None,*,content=None,params=None,raw=False):
        key=uuid4().hex
        try:
            r=self.client.get(path) if body is None and content is None else self.client.post(path,json=body,content=content,params=params,headers={'Idempotency-Key':key})
        except httpx.TransportError:
            raise ValueError('API连接失败或结果未知；未自动重试，请先用status检查任务。') from None
        if r.status_code>=400:
            try:code=r.json().get('code','invalid_request')
            except ValueError:code='invalid_response'
            raise ValueError(f'API {r.status_code}: {code}')
        return r.text if raw else r.json()

    def base(self):
        if not self.saved.get('goal_id'):raise ValueError('请先goal-create或goal-use选择目标。')
        return '/api/career-goals/'+str(UUID(self.saved['goal_id']))

    def materials(self):return self.request(self.base()+'/materials')

    def version(self):
        v=self.materials();return {'expected_version':v['goal_version'],'expected_revision':v['material_revision']}

    def draft(self,identity):
        v=self.materials();identity=str(UUID(identity))
        d=next((d for d in v['drafts'] if d['id']==identity),None)
        if d is None:raise ValueError('草稿不在最近30条列表中。')
        return v,d

    def interview_base(self):
        sid=self.saved.get('interviews',{}).get(self.saved.get('goal_id'))
        if not sid:raise ValueError('请先interview-start或interview-use选择面试。')
        return self.base()+'/interviews/'+str(UUID(sid))

    def execute(self,a,password=None):
        op=a.command
        if op in ('login','register'):
            self.client.cookies.clear()
            body={'email':a.email,'password':password if password is not None else getpass.getpass('密码：')}
            if op=='register':body['name']=a.name
            r=self.request('/api/auth/'+('sign-in' if op=='login' else 'sign-up')+'/email',body)
            self.saved={};self.save();return {'user':r['user']}
        if op=='logout':
            self.request('/api/auth/sign-out',{});self.client.cookies.clear();self.saved={};self.save();return {'signed_out':True}
        if op=='goals':return self.request('/api/career-goals/')
        if op=='goal-create':
            message={'training':'进行岗位面试训练','materials':'优化我的项目简历','research':'研究适合我的岗位'}[a.intent]
            g=self.request('/api/career-goals/',{'message':message,'intent':a.intent,'conditions':{'direction':a.direction,'cities':a.city,'city_policy':a.city_policy,'recruitment':a.recruitment},'jd_text':a.jd.read_text(encoding='utf-8-sig') if a.jd else ''})
            self.saved['goal_id']=g['id'];self.save();return {'goal_id':g['id']}
        if op=='goal-use':
            g=self.request('/api/career-goals/'+str(UUID(a.goal_id)));self.saved['goal_id']=g['id'];self.save();return {'goal_id':g['id']}
        if op=='applications':return self.request(self.base()+'/applications')
        if op=='application-create':
            v=self.version();body={'expected_version':v['expected_version'],'expected_material_revision':v['expected_revision'],'company':a.company,'title':a.title,'application_url':a.url}
            if a.draft_id:body['draft_id']=str(UUID(a.draft_id))
            if a.source_id:body['source_id']=str(UUID(a.source_id))
            return self.request(self.base()+'/applications',body)
        if op in ('application-show','application-review','application-edit-file'):
            aid=str(UUID(a.application_id));app=self.request(self.base()+'/applications/'+aid)
            if op=='application-show':return app
            if op=='application-review':
                result={'application_id':aid,'version':app['version'],'status':app['status'],'package_revision':app['current_package_revision'],'review':app['review']}
                if a.output:
                    with a.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2,default=str)
                return result
            body={'expected_application_version':app['version']}
            if a.kind=='progress':body.update(status=app['status'],note='',evidence_kind='user_confirmation',receipt_reference='',is_correction=False,interview_round='')
            elif a.kind=='availability':body.update(availability='unknown',note='')
            else:
                v=self.version();p=app['package'];material=p['material']
                body.update(expected_version=v['expected_version'],expected_material_revision=v['expected_revision'],company=p['company'],title=p['title'],application_url=p['entry_url'])
                if material['kind'] in ('draft','source'):body[material['kind']+'_id']=material['id']
            doc={'goal_id':self.saved['goal_id'],'application_id':aid,'kind':a.kind,'command':body}
            with a.output.open('x',encoding='utf-8') as f:json.dump(doc,f,ensure_ascii=False,indent=2)
            return {'edit_file':str(a.output),'message':'编辑command后用application-record提交；保留版本号，填写实际反馈/回执说明。'}
        if op=='application-record':
            doc=json.loads(a.file.read_text(encoding='utf-8-sig'))
            if doc['goal_id']!=self.saved.get('goal_id'):raise ValueError('申请记录文件不属于当前目标。')
            endpoint={'progress':'progress','package':'packages','availability':'availability'}.get(doc['kind'])
            if endpoint is None:raise ValueError('unsupported_application_command')
            return self.request(self.base()+'/applications/'+str(UUID(doc['application_id']))+'/'+endpoint,doc['command'])
        if op=='jobs':return self.request(self.base()+'/jobs')
        if op=='research':
            from interview_agent.career.research_tools import ResearchInput
            g=self.request(self.base())
            body=ResearchInput(expected_version=g['version'],kind=a.kind,source_id=a.source,keyword=a.keyword,job_id=a.job_id).model_dump(mode='json')
            return self.request(self.base()+'/research',body)
        if op=='job-choice-file':
            v=self.request(self.base()+'/jobs');run=str(UUID(a.run_id));snapshot=str(UUID(a.snapshot_id))
            item=next((i for i in v['items'] if i['run_id']==run and i['snapshot_id']==snapshot),None)
            if item is None:raise ValueError('未找到当前目标版本下的岗位记录。')
            doc={'goal_id':self.saved['goal_id'],'job':item,'command':{'expected_version':v['goal_version'],'run_id':run,'snapshot_id':snapshot,'acknowledge_unknown':False}}
            with a.output.open('x',encoding='utf-8') as f:json.dump(doc,f,ensure_ascii=False,indent=2,default=str)
            return {'choice_file':str(a.output),'message':'阅读岗位来源和匹配状态；仅确认接受未知项时将command.acknowledge_unknown设为true，再用job-select提交。'}
        if op=='job-select':
            doc=json.loads(a.file.read_text(encoding='utf-8-sig'))
            if doc['goal_id']!=self.saved.get('goal_id'):raise ValueError('选岗文件不属于当前目标。')
            return self.request(self.base()+'/selection',doc['command'])
        if op == 'question-banks':
            return self.request('/api/question-banks')
        if op == 'question-bank-preview':
            with a.file.open('rb') as source:
                data = source.read(1_048_577)
            if len(data) > 1_048_576:
                raise ValueError('题库 JSON 文件不能超过 1 MiB。')
            document = json.loads(data.decode('utf-8-sig'))
            return self.request('/api/question-banks/imports', {'document': document})
        if op == 'question-bank-commit':
            preview_id = str(UUID(a.preview_id))
            update = a.update
            body = {'bank_id': update.bank_id, 'expected_version': update.version} if update else {}
            return self.request(f'/api/question-banks/imports/{preview_id}/commit', body)
        if op=='interviews':return self.request(self.base()+'/interviews')
        if op in ('interview-start','interview-use'):
            if op=='interview-start':
                goal=self.request(self.base())
                body={'expected_version':goal['version'],'role':a.role,'mode':a.mode,'max_questions':a.questions,'duration_seconds':a.seconds}
                if a.draft_id:body['draft_id']=str(UUID(a.draft_id))
                if a.application_id:body['application_id']=str(UUID(a.application_id))
                if a.package_revision is not None:body['application_package_revision']=a.package_revision
                if a.interview_round:body['interview_round']=a.interview_round
                if a.resume_item:body['resume_item_indexes']=a.resume_item
                if a.question_bank:
                    body['question_banks'] = [bank.model_dump(mode='json') for bank in a.question_bank]
                s=self.request(self.base()+'/interviews',body)
            else:s=self.request(self.base()+'/interviews/'+str(UUID(a.session_id)))
            self.saved.setdefault('interviews',{})[self.saved['goal_id']]=s['id'];self.save();return s
        if op=='interview-show':return self.request(self.interview_base())
        if op=='interview-answer-file':
            s=self.request(self.interview_base())
            if s['status']!='WAITING_FOR_USER':raise ValueError('当前不在等待回答状态，请先interview-show。')
            q=next(e for e in reversed(s['events']) if e['kind']=='question')
            doc={'goal_id':self.saved['goal_id'],'session_id':s['id'],'question':q['payload'],
                 'command':{'expected_version':s['version'],'question_id':q['id'],'text':''}}
            with a.output.open('x',encoding='utf-8') as f:json.dump(doc,f,ensure_ascii=False,indent=2)
            return {'answer_file':str(a.output),'message':'在command.text填写回答，再用interview-submit提交；题目与版本已冻结。'}
        if op=='interview-submit':
            doc=json.loads(a.file.read_text(encoding='utf-8-sig'))
            if doc['goal_id']!=self.saved.get('goal_id') or doc['session_id']!=self.saved.get('interviews',{}).get(self.saved.get('goal_id')):
                raise ValueError('回答文件不属于当前目标/面试。')
            from pydantic import ValidationError
            from interview_agent.interviews.contracts import Answer

            try:
                answer = Answer.model_validate(doc['command'])
            except ValidationError as error:
                invalid_fields = {item['loc'] for item in error.errors(include_input=False)}
                if ('text',) in invalid_fields:
                    raise ValueError('请在回答文件的 command.text 填写并保存 1–1500 个字符的答案。') from None
                raise ValueError('回答文件的 command 格式无效，请生成当前题的新回答文件。') from None
            return self.request(self.interview_base()+'/answers', answer.model_dump(mode='json'))
        if op=='interview-stop':
            s=self.request(self.interview_base())
            return self.request(self.interview_base()+'/stop',{'expected_version':s['version']})
        if op=='interview-report':
            s=self.request(self.interview_base());result={'session_id':s['id'],'status':s['status'],'report':s['report']}
            if a.output:
                if s['report'] is None:raise ValueError('报告尚未生成，请稍后查看或先结束面试。')
                with a.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2,default=str)
            return result
        if op=='upload':
            if a.file.suffix.lower() not in ('.txt','.md'):raise ValueError('文本上传支持.txt/.md；PDF/DOCX请使用preview和import-preview。')
            return self.request(self.base()+'/materials/sources',{**self.version(),'name':a.file.name,'format':'markdown' if a.file.suffix.lower()=='.md' else 'text','text':a.file.read_text(encoding='utf-8-sig')})
        if op=='preview':
            return self.request(self.base()+'/materials/file-previews',content=a.file.read_bytes(),params={**self.version(),'filename':a.file.name})
        if op=='import-preview':
            return self.request(self.base()+'/materials/file-previews/'+str(UUID(a.preview_id))+'/import',self.version())
        if op=='rewrite':
            v=self.request(self.base()+'/materials/rewrite',self.version());return {'run_id':v['run_id'],'message':'任务已提交；请运行已配置的材料Worker，用status查看结果。'}
        if op=='status':
            v=self.materials();return {'goal_id':self.saved['goal_id'],'material_revision':v['material_revision'],'runs':v['runs'],'drafts':[{'id':d['id'],'version':d['version'],'stale':d['stale'],'status':d['payload'].get('candidate_status')} for d in v['drafts']]}
        if op=='show':
            _,d=self.draft(a.draft_id);return {'draft_id':d['id'],'version':d['version'],'stale':d['stale'],'resume':d['markdown'],'items':d['payload'].get('rewrite_items',[]),'suggestions':d['payload'].get('suggestions',[])}
        if op=='edit-file':
            v,d=self.draft(a.draft_id)
            if d['payload'].get('draft_style')!='rewrite':raise ValueError('仅支持改写草稿。')
            doc={'goal_id':self.saved['goal_id'],'draft_id':d['id'],'command':{'expected_version':v['goal_version'],'expected_revision':v['material_revision'],'expected_draft_version':max(x['version'] for x in v['drafts']),'action':'edit','items':d['payload']['rewrite_items'],'selected_suggestion_indexes':[]}}
            with a.output.open('x',encoding='utf-8') as f:json.dump(doc,f,ensure_ascii=False,indent=2)
            return {'edit_file':str(a.output),'message':'编辑items中的text和change_note；建议序号从0起，选中建议不会自动加入正文。用save-edit提交。'}
        if op=='save-edit':
            doc=json.loads(a.file.read_text(encoding='utf-8-sig'))
            if doc['goal_id']!=self.saved.get('goal_id') or doc['command']['action']!='edit':raise ValueError('编辑文件的目标或动作不匹配。')
            r=self.request(self.base()+'/materials/drafts/'+str(UUID(doc['draft_id']))+'/revisions',doc['command']);return {'draft_id':r['draft_id']}
        if op in ('accept','restore'):
            v,d=self.draft(a.draft_id)
            r=self.request(self.base()+f"/materials/drafts/{d['id']}/revisions",{'expected_version':v['goal_version'],'expected_revision':v['material_revision'],'expected_draft_version':max(x['version'] for x in v['drafts']),'action':op});return {'draft_id':r['draft_id']}
        if op=='export':
            md=self.request(self.base()+'/materials/drafts/'+str(UUID(a.draft_id))+'.md',raw=True)
            with a.output.open('x',encoding='utf-8') as f:f.write(md)
            return {'exported':str(a.output)}
        raise ValueError('unsupported_command')


def bank_ref(value):
    from interview_agent.question_banks.contracts import BankRef

    bank_id, separator, version = value.partition('@')
    if not separator or not version.isascii() or not version.isdecimal():
        raise argparse.ArgumentTypeError('请使用 题库UUID@版本，例如 UUID@1。')
    try:
        return BankRef(bank_id=bank_id, version=int(version))
    except ValueError:
        raise argparse.ArgumentTypeError('题库 ID 或版本无效，请检查 UUID 和版本号。') from None


def parser():
    p=argparse.ArgumentParser(description='本地简历助手：复用FastAPI/Worker的日常命令入口。')
    p.add_argument('--state',type=Path,default=Path('.private/resume/session.json'),help='本机登录会话，必须保存在.private内；请勿提交或分享。')
    s=p.add_subparsers(dest='command',required=True)
    for name in ('login','register'):
        c=s.add_parser(name);c.add_argument('--email',required=True)
        if name=='register':c.add_argument('--name',required=True)
    for name in ('logout','goals','rewrite','status','jobs','applications','interviews','interview-show','interview-stop'):s.add_parser(name)
    c=s.add_parser('application-create');c.add_argument('--company',required=True);c.add_argument('--title',required=True);c.add_argument('--url',default='');g=c.add_mutually_exclusive_group();g.add_argument('--draft-id');g.add_argument('--source-id')
    c=s.add_parser('application-show');c.add_argument('application_id')
    c=s.add_parser('application-review');c.add_argument('application_id');c.add_argument('--output',type=Path)
    c=s.add_parser('application-edit-file');c.add_argument('application_id');c.add_argument('--kind',choices=['progress','package','availability'],required=True);c.add_argument('--output',type=Path,required=True)
    c=s.add_parser('application-record');c.add_argument('file',type=Path)
    c=s.add_parser('research');c.add_argument('--kind',choices=['search','detail','import_jd'],required=True);c.add_argument('--source',choices=['tme-campus','xiaomi-intern'],default='tme-campus');c.add_argument('--keyword',default='');c.add_argument('--job-id',default='')
    c=s.add_parser('job-choice-file');c.add_argument('--run-id',required=True);c.add_argument('--snapshot-id',required=True);c.add_argument('--output',type=Path,required=True)
    c=s.add_parser('job-select');c.add_argument('file',type=Path)
    c=s.add_parser('interview-start');c.add_argument('--role',choices=['java-backend','ai-application'],required=True);c.add_argument('--mode',choices=['interview','coaching'],default='interview');c.add_argument('--questions',type=int,default=4);c.add_argument('--seconds',type=int,default=1200);g=c.add_mutually_exclusive_group();g.add_argument('--draft-id');g.add_argument('--application-id');c.add_argument('--package-revision',type=int);c.add_argument('--interview-round',default='');c.add_argument('--resume-item',type=int,action='append',default=[])
    c.add_argument('--question-bank', type=bank_ref, action='append', default=[], metavar='UUID@VERSION',
                   help='选择固定题库版本，可指定最多两次。')
    s.add_parser('question-banks', help='列出内置题库和当前账号的私有题库。')
    c = s.add_parser('question-bank-preview', help='预览题库 JSON，返回待确认的预览 ID。')
    c.add_argument('file', type=Path)
    c = s.add_parser('question-bank-commit', help='确认题库预览并发布版本。')
    c.add_argument('preview_id')
    c.add_argument('--update', type=bank_ref, metavar='UUID@VERSION',
                   help='用指定的当前版本发布新版，省略时新建题库。')
    c=s.add_parser('interview-use');c.add_argument('session_id')
    c=s.add_parser('interview-answer-file');c.add_argument('--output',type=Path,required=True)
    c=s.add_parser('interview-submit');c.add_argument('file',type=Path)
    c=s.add_parser('interview-report');c.add_argument('--output',type=Path)
    c=s.add_parser('goal-create');c.add_argument('--direction',required=True);c.add_argument('--jd',type=Path);c.add_argument('--intent',choices=['materials','training','research'],default='materials');c.add_argument('--city',action='append',default=[]);c.add_argument('--city-policy',choices=['unknown','preferred','required'],default='unknown');c.add_argument('--recruitment',choices=['unknown','intern','campus','experienced'],default='unknown')
    c=s.add_parser('goal-use');c.add_argument('goal_id')
    for name in ('upload','preview','save-edit'):
        c=s.add_parser(name);c.add_argument('file',type=Path)
    c=s.add_parser('import-preview');c.add_argument('preview_id')
    for name in ('show','accept','restore','edit-file','export'):
        c=s.add_parser(name);c.add_argument('draft_id')
        if name in ('edit-file','export'):c.add_argument('--output',type=Path,required=True)
    return p


def main(argv=None):
    a=parser().parse_args(argv)
    try:
        with httpx.Client(base_url=ORIGIN,timeout=30,trust_env=False,follow_redirects=False) as client:
            result=ResumeClient(client,a.state).execute(a)
        # Keep JSON pipelines portable across legacy Windows code pages.
        print(json.dumps(result, ensure_ascii=not sys.stdout.isatty(), indent=2))
        return 0
    except (ValueError,OSError,KeyError) as error:
        print(str(error));return 1


if __name__=='__main__':raise SystemExit(main())
