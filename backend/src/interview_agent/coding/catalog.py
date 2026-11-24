"""Authored miniature repositories. Full cases stay outside the assistant's tools."""
from copy import deepcopy
from interview_agent.career.repository import digest

VERSION = 'ja07-mini-java-v1'
MAIN_CACHE = '''import java.util.Scanner;
public class Main {
    public static void main(String[] args) {
        var cache = new ExpiringCache();
        var scanner = new Scanner(System.in);
        while (scanner.hasNextLine()) {
            String[] p = scanner.nextLine().split(" ");
            if (p[0].equals("PUT")) {
                cache.put(p[1], p[2], Long.parseLong(p[3]), Long.parseLong(p[4]));
                System.out.println("OK");
            } else System.out.println(cache.get(p[1], Long.parseLong(p[2])));
        }
    }
}
'''
CACHE = '''import java.util.HashMap;
class ExpiringCache {
    private record Entry(String value, long expiresAt) {}
    private final HashMap<String, Entry> entries = new HashMap<>();
    void put(String key, String value, long now, long ttl) {
        entries.put(key, new Entry(value, now + ttl));
    }
    String get(String key, long now) {
        Entry e = entries.get(key);
        if (e == null) return "MISS";
        if (e.expiresAt() < now) {
            entries.remove(key);
            return "MISS";
        }
        return e.value();
    }
}
'''
MAIN_ORDERS = '''import java.util.Scanner;
public class Main {
    public static void main(String[] args) {
        var service = new OrderService();
        var scanner = new Scanner(System.in);
        while (scanner.hasNextLine()) System.out.println(service.handle(scanner.nextLine()));
    }
}
'''
ORDERS = '''import java.util.HashMap;
class OrderService {
    private final HashMap<String, OrderState> orders = new HashMap<>();
    String handle(String line) {
        String[] p = line.split(" ");
        String command = p[0], id = p[1];
        if (command.equals("CREATE")) {
            if (orders.containsKey(id)) return "EXISTS";
            orders.put(id, OrderState.NEW);
            return "NEW";
        }
        OrderState state = orders.get(id);
        if (state == null) return "NOT_FOUND";
        if (command.equals("STATUS")) return state.name();
        if (command.equals("PAY")) {
            if (state == OrderState.PAID) return "ALREADY_PAID";
            if (state != OrderState.NEW) return "INVALID_STATE";
            orders.put(id, OrderState.PAID);
            return "PAID";
        }
        // TODO: implement CANCEL without changing CREATE/PAY/STATUS behavior.
        return "UNSUPPORTED";
    }
}
'''


def case(identity, input, output, visibility='hidden'):
    return {'id': identity, 'input': input, 'output': output, 'visibility': visibility}


TASKS = {
    'ttl-boundary': {
        'title': '修复TTL缓存的过期边界',
        'statement': 'PUT key value now ttl记录绝对过期时刻now+ttl；GET key now在now>=过期时刻必须MISS，否则返回值。ttl可为0。时间与ttl非负且和不溢出，输入格式保证合法；替换同名key更新过期时刻。只修ExpiringCache.java，保留未过期命中与多key行为。',
        'files': {'README.md': 'Java21标准库小仓库；Main为固定stdin驱动，不允许修改。\n', 'Main.java': MAIN_CACHE, 'ExpiringCache.java': CACHE},
        'editable': ['ExpiringCache.java'],
        'cases': [
            case('expiry-boundary', 'PUT a value 10 5\nGET a 14\nGET a 15\n', 'OK\nvalue\nMISS\n', 'public'),
            case('zero-ttl', 'PUT a x 3 0\nGET a 3\n', 'OK\nMISS\n'),
            case('replacement', 'PUT a x 0 2\nPUT a y 1 20\nGET a 2\nGET a 21\n', 'OK\nOK\ny\nMISS\n'),
            case('independent-keys', 'PUT a x 0 2\nPUT b y 0 5\nGET a 3\nGET b 3\nGET z 3\n', 'OK\nOK\nMISS\ny\nMISS\n')],
    },
    'order-cancel': {
        'title': '为订单服务增加取消功能',
        'statement': '现有CREATE/PAY/STATUS保持行为。增加CANCEL id：NEW变CANCELLED并返回CANCELLED；重复取消返回ALREADY_CANCELLED；PAID返回INVALID_STATE且不变；不存在返回NOT_FOUND。取消后PAY应INVALID_STATE。只改OrderService.java，不修改驱动或枚举；输入格式合法，每行一个命令。',
        'files': {'README.md': '内存订单状态机小仓库，不代表数据库事务或真实支付系统。\n', 'Main.java': MAIN_ORDERS, 'OrderService.java': ORDERS, 'OrderState.java': 'enum OrderState { NEW, PAID, CANCELLED }\n'},
        'editable': ['OrderService.java'],
        'cases': [
            case('cancel-new', 'CREATE a\nCANCEL a\nSTATUS a\n', 'NEW\nCANCELLED\nCANCELLED\n', 'public'),
            case('cancel-repeat', 'CREATE a\nCANCEL a\nCANCEL a\nPAY a\n', 'NEW\nCANCELLED\nALREADY_CANCELLED\nINVALID_STATE\n'),
            case('paid-order', 'CREATE a\nPAY a\nCANCEL a\nSTATUS a\nPAY a\n', 'NEW\nPAID\nINVALID_STATE\nPAID\nALREADY_PAID\n'),
            case('existing-behavior', 'CANCEL z\nCREATE a\nCREATE a\nCREATE b\nSTATUS b\nCANCEL a\nSTATUS b\n', 'NOT_FOUND\nNEW\nEXISTS\nNEW\nNEW\nCANCELLED\nNEW\n')],
    },
}


def task(identity):
    if identity not in TASKS:
        raise ValueError('coding_task_unavailable')
    value = deepcopy(TASKS[identity]); value.update(id=identity, version=VERSION)
    value['sha256'] = digest(value)
    return value


def public_task(value):
    return {**{k: deepcopy(value[k]) for k in ('id', 'version', 'sha256', 'title', 'statement', 'editable')},
            'samples': [deepcopy(c) for c in value['cases'] if c['visibility'] == 'public']}
