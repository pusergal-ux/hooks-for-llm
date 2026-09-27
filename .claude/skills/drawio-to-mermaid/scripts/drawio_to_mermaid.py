#!/usr/bin/env python3
"""
drawio_to_mermaid.py

Геометрический парсер .drawio -> Mermaid flowchart.

Задача: в отличие от drawio-digest и подобных структурных парсеров,
которые читают ТОЛЬКО formal source/target у mxCell, этот скрипт
дополнительно резолвит стрелки, у которых нет formal-привязки к
фигурам (архитектор визуально подвёл линию, но не "приклеил" её
в drawio), используя эвристику по координатам:

  1. Если у ребра есть source/target -> используем как есть (надёжно).
  2. Если source/target нет -> берём geometry-точки конца линии
     (mxPoint as="sourcePoint"/"targetPoint") и ищем ближайшую
     фигуру (vertex) по расстоянию от точки до bounding box.
  3. При наложении нескольких фигур-кандидатов на одной позиции -
     приоритет отдаётся последней по порядку в XML (обычно то,
     что нарисовано позже, визуально наверху и вероятнее то,
     к чему "прицеливался" автор стрелки).

Использование:
    python3 drawio_to_mermaid.py diagram.drawio
    python3 drawio_to_mermaid.py diagram.drawio --threshold 40
    python3 drawio_to_mermaid.py diagram.drawio --format json
    python3 drawio_to_mermaid.py diagram.drawio --debug   # печатает эвристические решения

Формат .drawio:
    Файл может содержать XML в открытом виде (<mxGraphModel>...)
    либо сжатую строку внутри <diagram>...</diagram>
    (base64 + deflate, стандартный экспорт draw.io). Скрипт
    обрабатывает оба варианта автоматически.
"""

import argparse
import base64
import json
import re
import sys
import zlib
from dataclasses import dataclass, field
from urllib.parse import unquote
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------
# Модели данных
# --------------------------------------------------------------------------

@dataclass
class Shape:
    id: str
    label: str
    x: float
    y: float
    w: float
    h: float
    order: int  # позиция в XML — для z-order эвристики

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2

    def distance_to_point(self, px, py):
        """Расстояние от точки до ближайшей границы bounding box.
        0, если точка внутри фигуры."""
        dx = max(self.x - px, 0, px - self.x2)
        dy = max(self.y - py, 0, py - self.y2)
        return (dx ** 2 + dy ** 2) ** 0.5

    def contains(self, px, py, margin=0.0):
        return (self.x - margin <= px <= self.x2 + margin and
                self.y - margin <= py <= self.y2 + margin)


@dataclass
class Edge:
    id: str
    label: str
    source_id: str | None
    target_id: str | None
    source_point: tuple | None = None  # (x, y) — если нет formal source
    target_point: tuple | None = None
    resolved_source: str | None = field(default=None)
    resolved_target: str | None = field(default=None)
    resolution_note: str = ""  # для --debug
    # Кандидаты, рассмотренные эвристикой для каждой точки (заполняется
    # только если для этой стороны НЕ было formal source/target).
    # Каждый элемент: {"id", "label", "distance"}. Нужно для передачи
    # в LLM-промпт семантического резолвинга (см. semantic_resolution_prompt.md).
    source_candidates: list = field(default_factory=list)
    target_candidates: list = field(default_factory=list)
    source_overlap: bool = False  # True если у этой точки было наложение (tie по расстоянию)
    target_overlap: bool = False


# --------------------------------------------------------------------------
# Декомпрессия .drawio
# --------------------------------------------------------------------------

def load_xml_root(path: str) -> ET.Element:
    """Читает .drawio файл и возвращает корневой XML-элемент
    (уже раскодированной/распакованной диаграммы)."""
    raw = open(path, "r", encoding="utf-8").read()

    # Если файл уже содержит открытый mxGraphModel — парсим напрямую
    if "<mxGraphModel" in raw:
        return ET.fromstring(raw)

    # Иначе ищем <diagram>...</diagram> со сжатым содержимым
    m = re.search(r"<diagram[^>]*>(.*?)</diagram>", raw, re.DOTALL)
    if not m:
        raise ValueError(
            "Не найден ни <mxGraphModel>, ни <diagram> в файле. "
            "Проверьте, что это валидный экспорт draw.io (.drawio/.xml)."
        )

    payload = m.group(1).strip()
    decoded = decode_drawio_payload(payload)
    return ET.fromstring(decoded)


def decode_drawio_payload(payload: str) -> str:
    """Стандартная схема draw.io: base64 -> raw deflate -> URL-decode."""
    data = base64.b64decode(payload)
    # draw.io использует raw deflate (без zlib header) -> wbits=-15
    inflated = zlib.decompress(data, wbits=-15)
    xml_text = unquote(inflated.decode("utf-8"))
    return xml_text


# --------------------------------------------------------------------------
# Извлечение фигур и рёбер
# --------------------------------------------------------------------------

def parse_shapes_and_edges(root: ET.Element):
    shapes: dict[str, Shape] = {}
    edges: list[Edge] = []

    cells = root.findall(".//mxCell")
    for order, cell in enumerate(cells):
        style = cell.get("style", "") or ""
        value = clean_label(cell.get("value", ""))
        cell_id = cell.get("id")

        is_edge = cell.get("edge") == "1"
        is_vertex = cell.get("vertex") == "1"

        geom = cell.find("mxGeometry")

        if is_vertex and geom is not None:
            x = float(geom.get("x", 0))
            y = float(geom.get("y", 0))
            w = float(geom.get("width", 0))
            h = float(geom.get("height", 0))
            # пропускаем нулевые/пустые фигуры-контейнеры без подписи —
            # часто это группирующие рамки (swimlane-обёртки), не концепты
            if value or (w > 0 and h > 0):
                shapes[cell_id] = Shape(cell_id, value, x, y, w, h, order)

        elif is_edge:
            source_id = cell.get("source")
            target_id = cell.get("target")
            source_point = target_point = None

            if geom is not None:
                sp = geom.find("mxPoint[@as='sourcePoint']")
                tp = geom.find("mxPoint[@as='targetPoint']")
                if sp is not None:
                    source_point = (float(sp.get("x", 0)), float(sp.get("y", 0)))
                if tp is not None:
                    target_point = (float(tp.get("x", 0)), float(tp.get("y", 0)))

            edges.append(Edge(
                id=cell_id,
                label=value,
                source_id=source_id,
                target_id=target_id,
                source_point=source_point,
                target_point=target_point,
            ))

    return shapes, edges


def clean_label(raw_value: str) -> str:
    """drawio часто хранит label как HTML (<b>...</b>, <div>...</div>).
    Убираем теги, схлопываем пробелы."""
    if not raw_value:
        return ""
    text = re.sub(r"<br\s*/?>", " ", raw_value)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


# --------------------------------------------------------------------------
# Эвристика привязки несвязанных рёбер
# --------------------------------------------------------------------------

def resolve_endpoint(point, shapes: dict, threshold: float, debug_notes: list, which: str):
    """Находит фигуру-кандидата для одной точки конца линии.
    При нескольких фигурах в радиусе threshold — берёт с наибольшим
    order (позже нарисована = вероятнее то, к чему целились).

    Возвращает (chosen_id | None, candidates_list, overlap_bool).
    candidates_list — ВСЕ фигуры в радиусе threshold, отсортированные
    по расстоянию, в виде словарей {id, label, distance} — это то,
    что уходит в JSON для последующего семантического резолвинга LLM
    (см. semantic_resolution_prompt.md), даже если сам скрипт смог
    выбрать кандидата эвристикой."""
    if point is None:
        return None, [], False

    px, py = point
    raw_candidates = []
    for shape in shapes.values():
        d = shape.distance_to_point(px, py)
        if d <= threshold:
            raw_candidates.append((d, shape))

    if not raw_candidates:
        return None, [], False

    raw_candidates.sort(key=lambda c: c[0])
    candidates_list = [
        {"id": s.id, "label": s.label or s.id, "distance": round(d, 1)}
        for d, s in raw_candidates
    ]

    # Сперва по расстоянию (точнее совпадение — приоритет), но если
    # несколько фигур практически на одинаковом расстоянии (наложение,
    # разница < 5px) — выбираем по z-order (позже в XML) как разумный
    # дефолт; сам факт наложения помечаем overlap=True, чтобы вызывающий
    # код мог отправить это на семантическую проверку LLM.
    best_distance = raw_candidates[0][0]
    tied = [c for c in raw_candidates if c[0] - best_distance < 5.0]
    tied.sort(key=lambda c: c[1].order, reverse=True)
    chosen = tied[0][1]
    overlap = len(tied) > 1

    if overlap:
        debug_notes.append(
            f"  [{which}] наложение {len(tied)} фигур у точки ({px:.0f},{py:.0f}): "
            f"выбрана '{chosen.label or chosen.id}' (id={chosen.id}, z-order={chosen.order})"
        )
    return chosen.id, candidates_list, overlap


def resolve_edges(shapes: dict, edges: list, threshold: float, debug: bool):
    debug_notes = []
    unresolved = []

    for edge in edges:
        # formal source/target — доверяем как есть
        edge.resolved_source = edge.source_id if edge.source_id in shapes else None
        edge.resolved_target = edge.target_id if edge.target_id in shapes else None

        if edge.resolved_source and edge.resolved_target:
            edge.resolution_note = "formal (source/target из XML)"
            continue

        # эвристика по координатам для недостающей стороны
        if not edge.resolved_source:
            sid, candidates, overlap = resolve_endpoint(
                edge.source_point, shapes, threshold, debug_notes, "source")
            edge.source_candidates = candidates
            edge.source_overlap = overlap
            if sid:
                edge.resolved_source = sid

        if not edge.resolved_target:
            tid, candidates, overlap = resolve_endpoint(
                edge.target_point, shapes, threshold, debug_notes, "target")
            edge.target_candidates = candidates
            edge.target_overlap = overlap
            if tid:
                edge.resolved_target = tid

        if edge.resolved_source and edge.resolved_target:
            edge.resolution_note = "geometric heuristic"
        else:
            edge.resolution_note = "UNRESOLVED"
            unresolved.append(edge)

    if debug:
        if debug_notes:
            print("--- Эвристические решения по наложению ---", file=sys.stderr)
            for note in debug_notes:
                print(note, file=sys.stderr)
        if unresolved:
            print(f"--- Не удалось привязать {len(unresolved)} ребро/рёбер ---", file=sys.stderr)
            for e in unresolved:
                print(f"  edge id={e.id} label='{e.label}' "
                      f"src_point={e.source_point} tgt_point={e.target_point}", file=sys.stderr)

    return unresolved


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

def to_mermaid_id(raw_id: str) -> str:
    """mermaid не любит некоторые символы в id — санитизируем."""
    return "n" + re.sub(r"[^a-zA-Z0-9_]", "_", raw_id)


def render_mermaid(shapes: dict, edges: list) -> str:
    lines = ["flowchart LR"]

    used_shape_ids = set()
    for edge in edges:
        if edge.resolved_source and edge.resolved_target:
            used_shape_ids.add(edge.resolved_source)
            used_shape_ids.add(edge.resolved_target)

    # объявляем узлы (только те, что реально участвуют в связях —
    # изолированные декоративные фигуры пропускаем)
    for shape_id in used_shape_ids:
        shape = shapes[shape_id]
        label = shape.label.replace('"', "'") or shape.id
        lines.append(f'    {to_mermaid_id(shape_id)}["{label}"]')

    for edge in edges:
        if not (edge.resolved_source and edge.resolved_target):
            continue
        src = to_mermaid_id(edge.resolved_source)
        tgt = to_mermaid_id(edge.resolved_target)
        if edge.label:
            safe_label = edge.label.replace('"', "'")
            lines.append(f'    {src} -->|"{safe_label}"| {tgt}')
        else:
            lines.append(f"    {src} --> {tgt}")

    return "\n".join(lines)


def render_json(shapes: dict, edges: list) -> str:
    edges_out = []
    for e in edges:
        item = {
            "id": e.id,
            "label": e.label,
            "source": e.resolved_source,
            "target": e.resolved_target,
            "resolution": e.resolution_note,
        }
        # Кандидаты добавляем только там, где резолюция шла через
        # геометрию (heuristic/unresolved) — для formal-рёбер они
        # не считались и не нужны (там уже есть надёжный source/target).
        if e.resolution_note != "formal (source/target из XML)":
            if e.source_point is not None:
                item["source_point"] = list(e.source_point)
                item["source_candidates"] = e.source_candidates
                item["source_overlap"] = e.source_overlap
            if e.target_point is not None:
                item["target_point"] = list(e.target_point)
                item["target_candidates"] = e.target_candidates
                item["target_overlap"] = e.target_overlap
            # needs_review: сигнал для пайплайна — это ребро стоит
            # прогнать через semantic_resolution_prompt.md, даже если
            # эвристика формально что-то выбрала (resolved_source/target
            # заполнены), потому что решение неоднозначное.
            item["needs_review"] = (
                e.resolution_note == "UNRESOLVED"
                or e.source_overlap
                or e.target_overlap
            )
        edges_out.append(item)

    data = {
        "nodes": [
            {"id": s.id, "label": s.label, "x": s.x, "y": s.y, "w": s.w, "h": s.h}
            for s in shapes.values()
        ],
        "edges": edges_out,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    # консоль Windows по умолчанию не UTF-8 — кириллица в выводе ломается
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser =argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("drawio_file", help="Путь к .drawio/.xml файлу")
    parser.add_argument("--threshold", type=float, default=30.0,
                         help="Радиус поиска ближайшей фигуры к концу несвязанной линии, px (по умолчанию 30)")
    parser.add_argument("--format", choices=["mermaid", "json"], default="mermaid",
                         help="Формат вывода (по умолчанию mermaid)")
    parser.add_argument("--debug", action="store_true",
                         help="Печатать в stderr эвристические решения и нерезолвленные рёбра")
    parser.add_argument("-o", "--output", help="Файл для записи результата (по умолчанию stdout)")
    args = parser.parse_args()

    root = load_xml_root(args.drawio_file)
    shapes, edges = parse_shapes_and_edges(root)
    unresolved = resolve_edges(shapes, edges, args.threshold, args.debug)

    if args.format == "mermaid":
        output = render_mermaid(shapes, edges)
    else:
        output = render_json(shapes, edges)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output + "\n")
    else:
        print(output)

    if unresolved:
        print(f"\n[!] {len(unresolved)} ребро/рёбер не удалось привязать "
              f"даже эвристикой (используйте --debug для деталей, "
              f"или увеличьте --threshold).", file=sys.stderr)


if __name__ == "__main__":
    main()
