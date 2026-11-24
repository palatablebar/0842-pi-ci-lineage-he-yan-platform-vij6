"""批次血缘图：解析父批次投料、判定血缘完整性与连通分量。

- 每条批次记录给出 ``parent_lot``（形如 ``L4:200;L5:40``，单父可省略贡献量）。
- 血缘边按无向处理：同一连通分量内的批次共用上游，召回沿上下游双向闭包追踪。
- 父批次记录尚未到达时，边的另一端是“缺失父批次”，无法排除混入，
  对应分量只能判定为“待核”而不能直接证实干净。
"""

from app.errors import DomainIssue


def parse_parents(parent_lot):
    """把父批次投料文本解析为 ``{parent_lot_id: contribution}``。

    单父批次可只填编号，此时贡献量为 ``None``，由调用方按投入量补全。
    """
    text = (parent_lot or "").strip()
    if not text:
        return {}
    parents = {}
    for edge in text.split(";"):
        parent, separator, quantity = edge.partition(":")
        parent = parent.strip()
        if not parent:
            raise DomainIssue("BAD_PARENT_EDGE")
        if parent in parents:
            raise DomainIssue("BAD_PARENT_EDGE")
        if separator:
            qty_text = quantity.strip()
            if not qty_text.isdigit() or int(qty_text) <= 0:
                raise DomainIssue("BAD_PARENT_EDGE")
            parents[parent] = int(qty_text)
        else:
            parents[parent] = None
    return parents


class Genealogy:
    """当前已知批次构成的血缘图快照。"""

    def __init__(self, lots):
        # lots: {lot_id: {"lot_id", "parents": {parent: qty}, "qty_in", ...}}
        self.lots = lots
        self.adjacency = {lot_id: set() for lot_id in lots}
        self.missing = {lot_id: set() for lot_id in lots}
        for lot_id, item in lots.items():
            for parent in item["parents"]:
                if parent in lots:
                    self.adjacency[lot_id].add(parent)
                    self.adjacency[parent].add(lot_id)
                else:
                    self.missing[lot_id].add(parent)

    def complete_lots(self):
        """返回血缘完整的批次集合：自身及所有祖先记录都已到齐。

        父批次记录缺失，或经任意祖先链路仍有缺失父批次，都不算完整。
        环上的批次只要没有缺失父批次，同样视为完整（环不影响混入判定）。
        """
        complete = set()
        for start in self.lots:
            if start in complete:
                continue
            stack = [start]
            seen = set()
            ok = True
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                if self.missing.get(node):
                    ok = False
                for parent in self.lots[node]["parents"]:
                    if parent in self.lots and parent not in seen:
                        stack.append(parent)
            if ok:
                complete.update(seen)
        return complete

    def components(self):
        """返回无向连通分量列表，每个分量是批次编号集合。"""
        visited = set()
        groups = []
        for start in self.lots:
            if start in visited:
                continue
            group = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                group.add(node)
                stack.extend(self.adjacency[node] - visited)
            groups.append(group)
        return groups

    def component_of(self, lot_id):
        if lot_id not in self.lots:
            return set()
        result = set()
        stack = [lot_id]
        while stack:
            node = stack.pop()
            if node in result:
                continue
            result.add(node)
            stack.extend(self.adjacency[node] - result)
        return result
