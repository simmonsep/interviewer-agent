"""Small authored corpus, not company interview questions or an ability benchmark."""
import copy
from interview_agent.career.repository import digest

VERSION = 'ja06-java-backend-v1'
STARTER = '''import java.io.*;
import java.util.*;
public class Main {
    public static void main(String[] args) throws Exception {
        // Read standard input and write only the required output.
    }
}
'''

TASKS = {
    'idempotency-record': {
        'title': '幂等请求记录：重放与冲突', 'topic_id': 'http-idempotency',
        'statement': '实现内存中的请求记录。首行n，随后n行key payload，均为不含空白的ASCII串。首次key输出NEW并保存payload；同key同payload输出REPLAY；同key不同payload输出CONFLICT，且不能覆盖最初payload。key区分大小写。1<=n<=1000，字符串长度1..40。每条请求输出一行。本题是状态转换练习，不等同于真实并发/持久化幂等系统。',
        'analysis': '先按key查找；不存在才保存。存在时比较原payload，冲突分支不要更新记录。复习：幂等键作用域、并发唯一约束和响应持久化仍需单独设计。',
        'cases': [
            {'id':'sample-1','visibility':'public','input':'4\na x\na x\na y\na x\n','output':'NEW\nREPLAY\nCONFLICT\nREPLAY\n'},
            {'id':'hidden-1','visibility':'hidden','input':'6\na x\nb x\na y\nb y\na x\nb x\n','output':'NEW\nNEW\nCONFLICT\nCONFLICT\nREPLAY\nREPLAY\n'},
            {'id':'hidden-2','visibility':'hidden','input':'5\na x\nA x\na X\nA x\na x\n','output':'NEW\nNEW\nCONFLICT\nREPLAY\nREPLAY\n'},
            {'id':'hidden-3','visibility':'hidden','input':'1\norder_1 hash_1\n','output':'NEW\n'},
        ],
    },
    'lock-owner-expiry': {
        'title': '锁状态机：过期与归属释放', 'topic_id': 'redis-lock',
        'statement': '模拟单把锁。首行n，随后n条指令，时间t单调不降。SET t token ttl：空锁设置持有者和到期时间t+ttl，输出OK；有锁输出BUSY且不续期。RELEASE t token：当前token相同则清空并输出RELEASED，否则NOT_OWNER。GET t：输出当前token或EMPTY。每条指令执行前若t>=到期时间则先清空。初始为空。1<=n<=1000，0<=t<=10^9，1<=ttl<=10^6，token为1..40位不含空白ASCII串。每条指令输出一行。本题是单线程模型，不要求连接Redis，不证明实现了真实分布式锁。',
        'analysis': '先处理过期，再执行命令。释放必须比较当前持有者；旧持有者不得删除过期后被新持有者获得的锁。复习：真实Redis比较与删除必须原子执行，租约/故障恢复还需另议。',
        'cases': [
            {'id':'sample-1','visibility':'public','input':'5\nSET 0 a 5\nGET 4\nGET 5\nSET 5 b 5\nRELEASE 6 a\n','output':'OK\na\nEMPTY\nOK\nNOT_OWNER\n'},
            {'id':'hidden-1','visibility':'hidden','input':'7\nSET 0 a 2\nSET 1 b 9\nSET 2 b 3\nRELEASE 3 a\nGET 3\nRELEASE 4 b\nGET 4\n','output':'OK\nBUSY\nOK\nNOT_OWNER\nb\nRELEASED\nEMPTY\n'},
            {'id':'hidden-2','visibility':'hidden','input':'6\nRELEASE 0 x\nSET 1000000000 T 1000000\nSET 1000000000 t 1\nGET 1000000000\nRELEASE 1000000000 t\nGET 1000000000\n','output':'NOT_OWNER\nOK\nBUSY\nT\nNOT_OWNER\nT\n'},
            {'id':'hidden-3','visibility':'hidden','input':'4\nSET 0 a 1\nRELEASE 1 a\nGET 1\nSET 1 a 1\n','output':'OK\nNOT_OWNER\nEMPTY\nOK\n'},
        ],
    },
}


def task(identity):
    result = {'id': identity, 'version': VERSION, 'language': 'java21', 'starter': STARTER,
              'entrypoint':'Main.java / public class Main / default package / standard library only',
              'origin': 'project_authored_synthetic', **copy.deepcopy(TASKS[identity])}
    result['sha256'] = digest(result)
    return result


def public_task(value):
    return {**{k:value[k] for k in ('id','version','language','entrypoint','starter','title','topic_id','statement','origin','sha256')},
            'samples': [x for x in value['cases'] if x['visibility']=='public']}


def catalog(): return [public_task(task(identity)) for identity in TASKS]
