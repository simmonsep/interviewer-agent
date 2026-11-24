"""F07Z-166: deterministic role resolution for the auto-prepared training task.

The role is a content choice, so it is decided by code from what the user actually confirmed
(direction), what they selected (job title) and what they said this turn (message). A JD body is
deliberately excluded: a posting can mention Java without being a Java role.
"""
JAVA = ('java', 'java后端', 'java 后端', '后端开发', '服务端开发', 'spring boot', 'springboot')
AI = ('ai 应用', 'ai应用', 'agent', '智能体', '大模型应用', 'llm 应用', 'llm应用', 'ai 开发', 'ai开发')


def _hits(text, words):
    value = (text or '').casefold().replace('javascript', '')
    return any(word in value for word in words)


def resolve_role(*, direction='', title='', message=''):
    """Return the role both a preparation and the opened session can use, or None to ask once."""
    sources = (direction, title, message)
    java = any(_hits(text, JAVA) for text in sources)
    ai = any(_hits(text, AI) for text in sources)
    if java == ai:
        return None
    return 'java-backend' if java else 'ai-application'
