"""Conservative, evidence-linked checks, not generalized qualification understanding."""
import json
import re
from .contracts import Conditions

SKILLS={'Java':r'java','Python':r'python','SQL':r'sql','Redis':r'redis','Spring Boot':r'spring[ -]?boot',
        'MySQL':r'mysql','PostgreSQL':r'postgres(?:ql)?','Docker':r'docker','LangGraph':r'langgraph'}


def checks(job:dict,conditions:dict,version:int):
    user=Conditions.model_validate(conditions)
    requirement_path='requirements' if job.get('requirements') else 'visible_text'
    rows=[]
    def add(field,status,quote=None,path=None,*,hard=True,note='',value=None):
        rows.append({'field':field,'status':status,'hard':hard,'source_path':path or requirement_path,'source_quote':quote,
                     'user_ref':{'kind':'confirmed_condition','goal_version':version,'field':field,
                                 'value':getattr(user,field,None) if value is None else value},'note':note})
    cities=job.get('cities',[])
    if not user.cities:add('cities','not_requested',hard=False,note='未确认城市条件')
    else:
        state='unknown' if not cities else 'pass' if {x.removesuffix('市') for x in user.cities}.intersection(x.removesuffix('市') for x in cities) else 'conflict'
        add('cities',state,json.dumps(cities,ensure_ascii=False) if cities else None,'cities',hard=user.city_policy=='required',note='工作城市字段；偏好冲突不会作为硬门槛排除')
    label=job.get('recruitment_label') or ''
    kinds=set()
    if not re.search(r'非|不接受|不招',label):
        if '实习' in label:kinds.add('intern')
        if '应届' in label or '校招' in label:kinds.add('campus')
        if '社招' in label:kinds.add('experienced')
    add('recruitment','not_requested' if user.recruitment=='unknown' else 'unknown' if not kinds else 'pass' if user.recruitment in kinds else 'conflict',
        label or None,'recruitment_label',hard=user.recruitment!='unknown',note='未知/技术大咖等标签不推断成校招或实习')
    requirements=job.get('requirements') or job.get('visible_text') or ''
    clauses=[x.strip() for x in re.split(r'[。；;\n]',requirements) if x.strip()]
    def clause_for(pattern):
        for text in clauses:
            found=re.search(pattern,text)
            if found:
                # Negations, upper bounds and mixed optional wording need human review.
                ambiguous=bool(re.search(r'或|择业|未满|其他届|亦可|也可|不限|不要求|不接受|不接收|不招|排除|不含|非\s*20\d|除外|以下|以内|以前|之后|以后|之前|最多|至多|不超过|优先|最好|建议|加分|可选',text))
                return text,found,ambiguous
        return None,None,False
    quote,match,ambiguous=clause_for(r'(20\d{2})(?:年)?\s*[-~至—]\s*(20\d{2})(?:年)?(?:应届)?(?:毕业生|届)|(20\d{2})(?:年)?(?:应届)?(?:毕业生|届)')
    if match:
        allowed=set()
        for found in re.finditer(r'(20\d{2})(?:年)?\s*[-~至—]\s*(20\d{2})(?:年)?(?:应届)?(?:毕业生|届)|(20\d{2})(?:年)?(?:应届)?(?:毕业生|届)',quote):
            lo,hi,one=found.groups()
            if one:allowed.add(int(one))
            elif 0<=int(hi)-int(lo)<=10:allowed.update(range(int(lo),int(hi)+1))
            else:ambiguous=True
        add('graduation_year','unknown' if ambiguous or user.graduation_year is None else 'pass' if user.graduation_year in allowed else 'conflict',quote,note='仅解析明确届别/有限年份区间；复杂表达待核验')
    elif user.graduation_year is not None:add('graduation_year','unknown',note='JD未发现受支持的毕业范围，不能判断资格')
    for field,pattern in [('days_per_week',r'每周\s*(?:至少|不少于)?\s*(?:工作|到岗)?\s*([1-7一二三四五六七])\s*天'),
                          ('duration_months',r'(?:至少实习|实习至少|实习时长(?:至少|不少于)?|实习不少于)\s*(\d{1,2})\s*个?月')]:
        quote,match,ambiguous=clause_for(pattern)
        actual=getattr(user,field)
        if match:
            token=match[1];minimum=int(token) if token.isdigit() else '一二三四五六七'.index(token)+1
            add(field,'unknown' if ambiguous or actual is None else 'pass' if actual>=minimum else 'conflict',quote,note='只解析明确最少天数/月数；复杂或优先表述保持未知')
        elif actual is not None:add(field,'unknown',note='JD未提供可解析要求，需进一步核验')
    declared=' '.join(user.skills)
    for skill,pattern in SKILLS.items():
        pattern=r'(?<![A-Za-z0-9_])'+pattern+r'(?![A-Za-z0-9_])'
        quote=next((x for x in clauses if re.search(pattern,x,re.I)),None)
        if quote:
            add('skills','pass' if re.search(pattern,declared,re.I) else 'unknown',quote,hard=False,
                note=f'{skill}：仅对照用户自述，未声明不等于不会，也不是独立能力验证',value=user.skills)
    add('availability','unknown',path='availability',note='本来源仅证明详情可读，当前在招状态未确认')
    outcome='excluded' if any(x['hard'] and x['status']=='conflict' for x in rows) else 'needs_verification' if any(x['hard'] and x['status']=='unknown' for x in rows) else 'can_explore'
    return {'outcome':outcome,'checks':rows,'score':None}
