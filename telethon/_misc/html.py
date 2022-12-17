"""
Simple HTML -> Telegram entity parser.
"""
import struct
from collections import deque
from html import escape
from html.parser import HTMLParser
from typing import Iterable, Optional, Tuple, List

from .._misc import helpers
from .. import _tl


# Helpers from markdown.py
def _add_surrogate(text):
    return ''.join(
        ''.join(chr(y) for y in struct.unpack('<HH', x.encode('utf-16le')))
        if (0x10000 <= ord(x) <= 0x10FFFF) else x for x in text
    )


def _del_surrogate(text):
    return text.encode('utf-16', 'surrogatepass').decode('utf-16')


class HTMLToTelegramParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ''
        self.entities = []
        self._building_entities = {}
        self._open_tags = deque()
        self._open_tags_meta = deque()

    def handle_starttag(self, tag, attrs):
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)

        attrs = dict(attrs)
        EntityType = None
        args = {}
        if tag in ['strong', 'b']:
            EntityType = _tl.MessageEntityBold
        elif tag in ['em', 'i']:
            EntityType = _tl.MessageEntityItalic
        elif tag == 'u':
            EntityType = _tl.MessageEntityUnderline
        elif tag in ['del', 's']:
            EntityType = _tl.MessageEntityStrike
        elif tag == 'tg-spoiler':
            EntityType = _tl.MessageEntitySpoiler
        elif tag == 'blockquote':
            EntityType = _tl.MessageEntityBlockquote
        elif tag == 'code':
            try:
                # If we're in the middle of a <pre> tag, this <code> tag is
                # probably intended for syntax highlighting.
                #
                # Syntax highlighting is set with
                #     <code class='language-...'>codeblock</code>
                # inside <pre> tags
                pre = self._building_entities['pre']
                try:
                    pre.language = attrs['class'][len('language-'):]
                except KeyError:
                    pass
            except KeyError:
                EntityType = _tl.MessageEntityCode
        elif tag == 'pre':
            EntityType = _tl.MessageEntityPre
            args['language'] = ''
        elif tag == 'a':
            try:
                url = attrs['href']
            except KeyError:
                return
            if url.startswith('mailto:'):
                url = url[len('mailto:'):]
                EntityType = _tl.MessageEntityEmail
            elif self.get_starttag_text() == url:
                EntityType = _tl.MessageEntityUrl
            else:
                EntityType = _tl.MessageEntityTextUrl
                args['url'] = url
                url = None
            self._open_tags_meta.popleft()
            self._open_tags_meta.appendleft(url)

        if EntityType and tag not in self._building_entities:
            self._building_entities[tag] = EntityType(
                offset=len(self.text),
                # The length will be determined when closing the tag.
                length=0,
                **args)

    def handle_data(self, text):
        previous_tag = self._open_tags[0] if len(self._open_tags) > 0 else ''
        if previous_tag == 'a':
            if url := self._open_tags_meta[0]:
                text = url

        for tag, entity in self._building_entities.items():
            entity.length += len(text)

        self.text += text

    def handle_endtag(self, tag):
        try:
            self._open_tags.popleft()
            self._open_tags_meta.popleft()
        except IndexError:
            pass
        if entity := self._building_entities.pop(tag, None):
            self.entities.append(entity)


def parse(html: str) -> Tuple[str, List[_tl.TypeMessageEntity]]:
    """
    Parses the given HTML message and returns its stripped representation
    plus a list of the _tl.MessageEntity's that were found.

    :param html: the message with HTML to be parsed.
    :return: a tuple consisting of (clean message, [message entities]).
    """
    if not html:
        return html, []

    parser = HTMLToTelegramParser()
    parser.feed(_add_surrogate(html))
    text = helpers.strip_text(parser.text, parser.entities)
    return _del_surrogate(text), parser.entities


def unparse(text: str, entities: Iterable[_tl.TypeMessageEntity], _offset: int = 0,
            _length: Optional[int] = None) -> str:
    """
    Performs the reverse operation to .parse(), effectively returning HTML
    given a normal text and its _tl.MessageEntity's.

    :param text: the text to be reconverted into HTML.
    :param entities: the _tl.MessageEntity's applied to the text.
    :return: a HTML representation of the combination of both inputs.
    """
    if not text:
        return text
    elif not entities:
        return escape(text)

    text = _add_surrogate(text)
    if _length is None:
        _length = len(text)
    html = []
    last_offset = 0
    for i, entity in enumerate(entities):
        if entity.offset >= _offset + _length:
            break
        relative_offset = entity.offset - _offset
        if relative_offset > last_offset:
            html.append(escape(text[last_offset:relative_offset]))
        elif relative_offset < last_offset:
            continue

        skip_entity = False
        length = entity.length

        # If we are in the middle of a surrogate nudge the position by +1.
        # Otherwise we would end up with malformed text and fail to encode.
        # For example of bad input: "Hi \ud83d\ude1c"
        # https://en.wikipedia.org/wiki/UTF-16#U+010000_to_U+10FFFF
        while helpers.within_surrogate(text, relative_offset, length=_length):
            relative_offset += 1

        while helpers.within_surrogate(text, relative_offset + length, length=_length):
            length += 1

        entity_text = unparse(text=text[relative_offset:relative_offset + length],
                              entities=entities[i + 1:],
                              _offset=entity.offset, _length=length)
        entity_type = type(entity)

        if entity_type == _tl.MessageEntityBold:
            html.append(f'<strong>{entity_text}</strong>')
        elif entity_type == _tl.MessageEntityItalic:
            html.append(f'<em>{entity_text}</em>')
        elif entity_type == _tl.MessageEntityCode:
            html.append(f'<code>{entity_text}</code>')
        elif entity_type == _tl.MessageEntityUnderline:
            html.append(f'<u>{entity_text}</u>')
        elif entity_type == _tl.MessageEntityStrike:
            html.append(f'<del>{entity_text}</del>')
        elif entity_type == _tl.MessageEntityBlockquote:
            html.append(f'<blockquote>{entity_text}</blockquote>')
        elif entity_type == _tl.MessageEntityPre:
            if entity.language:
                html.append(
                    f"<pre>\n    <code class='language-{entity.language}'>\n        {entity_text}\n    </code>\n</pre>"
                )
            else:
                html.append(f'<pre><code>{entity_text}</code></pre>')
        elif entity_type == _tl.MessageEntityEmail:
            html.append('<a href="mailto:{0}">{0}</a>'.format(entity_text))
        elif entity_type == _tl.MessageEntityUrl:
            html.append('<a href="{0}">{0}</a>'.format(entity_text))
        elif entity_type == _tl.MessageEntityTextUrl:
            html.append(f'<a href="{escape(entity.url)}">{entity_text}</a>')
        elif entity_type == _tl.MessageEntityMentionName:
            html.append(f'<a href="tg://user?id={entity.user_id}">{entity_text}</a>')
        else:
            skip_entity = True
        last_offset = relative_offset + (0 if skip_entity else length)

    while helpers.within_surrogate(text, last_offset, length=_length):
        last_offset += 1

    html.append(escape(text[last_offset:]))
    return _del_surrogate(''.join(html))
