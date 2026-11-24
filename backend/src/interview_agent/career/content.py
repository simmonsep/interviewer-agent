"""Visible text extraction, not a proof that third-party text is trustworthy."""
from html.parser import HTMLParser
import re

VOID = {'br','hr','img','input','meta','link','wbr','source','area','base','embed','param','track','col'}
INVISIBLE = re.compile('[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\u00ad\U000e0000-\U000e007f]')


class VisibleText(HTMLParser):
    def __init__(self, *, excluded_tags=(), within_tags=(), within_classes=()):
        super().__init__(convert_charrefs=True)
        self.excluded_tags = excluded_tags
        self.within_tags = frozenset(within_tags)
        self.within_classes = frozenset(within_classes)
        self.stack = []
        self.parts = []

    def _visible(self):
        return (not any(hidden for _, hidden, _ in self.stack)
                and (not (self.within_tags or self.within_classes)
                     or any(region for _, _, region in self.stack)))

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        style = re.sub(r'/\*.*?\*/', '', attributes.get('style') or '', flags=re.S)
        hidden = (tag in {'script','style','template','noscript','svg'} or tag in self.excluded_tags
                  or 'hidden' in attributes
                  or (attributes.get('aria-hidden') or '').lower() == 'true'
                  or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[;\s]|$)|font-size\s*:\s*0(?:px)?(?:[;\s]|$)', style, re.I)))
        if self._visible() and tag in {'br','p','div','li'}:
            self.parts.append('\n')
        if tag not in VOID:
            region = tag in self.within_tags or bool(self.within_classes.intersection(
                (attributes.get('class') or '').split()))
            self.stack.append((tag, hidden, region))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        was_visible_in_region = bool(self.within_tags or self.within_classes) and self._visible()
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        block_end = tag in {'p', 'div', 'li'} or (was_visible_in_region and tag in {'article', 'section'})
        if block_end and (was_visible_in_region or self._visible()):
            self.parts.append('\n')

    def handle_data(self, value):
        if self._visible(): self.parts.append(value)


def visible_text(value: str, *, excluded_tags=(), within_tags=(), within_classes=()) -> str:
    parser = VisibleText(excluded_tags=excluded_tags, within_tags=within_tags, within_classes=within_classes)
    parser.feed(value)
    parser.close()
    return '\n'.join(line.strip() for line in INVISIBLE.sub('', ''.join(parser.parts)).splitlines() if line.strip())


def review_flags(value: str) -> list[str]:
    return ['instruction_shaped_text'] if re.search(r'ignore.{0,30}instructions|system\s*prompt|忽略.{0,12}指令|泄露.{0,12}密钥',value,re.I) else []
