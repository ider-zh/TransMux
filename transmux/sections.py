"""Local, deterministic chapter planning; no additional model calls."""
from bisect import bisect_right


def estimated_tokens(text):
    # A budgeting heuristic, not the tokenizer or context limit of any provider.
    return sum(1.5 if ord(char) > 127 else 1 / 3 for char in text)


class ChapterPlanner:
    def __init__(self, blocks, max_chars=10000, max_estimated_tokens=12000):
        self.blocks = blocks
        self.max_chars = max_chars
        self.max_tokens = max_estimated_tokens
        self.chars, self.tokens = [0], [0]
        self.paths = []
        root = {'start': 0, 'end': len(blocks), 'children': [], 'level': 0}
        stack = [root]
        for i, block in enumerate(blocks):
            text = block['text']
            if len(text) > 40000:
                raise ValueError('单段超过 4 万字符，请先拆分段落')
            self.chars.append(self.chars[-1] + len(text))
            # Reserve 1.5 times source tokens for translated output.
            self.tokens.append(self.tokens[-1] + estimated_tokens(text) * 2.5)
            if block['kind'] == 'heading':
                level = block['level']
                while len(stack) > 1 and stack[-1]['level'] >= level:
                    stack.pop()['end'] = i
                node = dict(start=i, end=len(blocks), children=[], level=level,
                            id=block['id'], title=text[:200])
                stack[-1]['children'].append(node)
                stack.append(node)
            self.paths.append([{'id': n['id'], 'title': n['title'], 'level': n['level']} for n in stack[1:]])

        def partition(node, root_node=False):
            start, end = node['start'], node['end']
            children = node['children']
            if not children or (not root_node and self.fits(start, end)):
                return [(start, end)] if start < end else []
            units = []
            intro_end = children[0]['start']
            if start < intro_end:
                units.append((start, intro_end))
            for child in children:
                units.extend(partition(child))
            # Don't spend an agent call on an ancestor heading alone. Attach it
            # to the first child unit; the window budget still applies below.
            if len(units) > 1 and all(b['kind'] == 'heading' for b in blocks[start:intro_end]) and start < intro_end:
                units[:2] = [(start, units[1][1])]
            return units

        self.units = partition(root, root_node=True)
        self.starts = [start for start, _ in self.units]

    def fits(self, start, end):
        return (self.chars[end] - self.chars[start] <= self.max_chars
                and self.tokens[end] - self.tokens[start] <= self.max_tokens)

    def window(self, offset):
        if not 0 <= offset < len(self.blocks):
            raise ValueError('无效翻译起始段落')
        _, limit = self.units[bisect_right(self.starts, offset) - 1]
        end = offset + 1  # An indivisible source paragraph may exceed the soft budget.
        while end < limit and self.fits(offset, end + 1):
            end += 1
        continuation = (end - offset >= 2 and end < limit
                        and not self.blocks[end - 1]['protected'] and not self.blocks[end]['protected']
                        and self.blocks[end - 1]['segment'] == self.blocks[end]['segment'])
        return self.blocks[offset:end], continuation

    def context(self, offset, end, approved):
        paths = self.paths[offset:end]
        common = list(paths[0])
        for path in paths[1:]:
            common = common[:min(len(common), len(path))]
            while common and common != path[:len(common)]:
                common.pop()
        top = self.paths[offset][:1]

        def neighbor(index):
            if not 0 <= index < len(self.blocks) or self.paths[index][:1] != top:
                return None
            block = self.blocks[index]
            text = block['text']
            return {'source_id': block['id'], 'text': text[-800:] if index < offset else text[:800],
                    'truncated': len(text) > 800}

        return {'section_path': common or self.paths[offset],
                'read_only_context': {'preceding_source': neighbor(offset - 1), 'following_source': neighbor(end),
                                      'preceding_translation': approved[-1]['translation'][-800:] if approved and neighbor(offset - 1) else None},
                'oversized_paragraph': not self.fits(offset, end)}
