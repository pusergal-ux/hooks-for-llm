#!/usr/bin/env python3
"""
drawio_to_mermaid.py

Геометрический парсер .drawio -> Mermaid flowchart.

Задача: в отличие от drawio-digest и подобных структурных парсеров,
которые читают ТОЛЬКО formal source/target у mxCell, этот скрипт
дополнительно резолвит стрелки, у которых нет formal-привязки к
фигурам, используя две отдельные эвристики:

  ЭВРИСТИКА 1 — привязка концов линии к фигурам (для рёбер вообще
  без formal source/target):
    Если у ребра есть source/target -> используем как есть (надёжно).
    Если нет -> берём geometry-точки конца линии (mxPoint
    as="sourcePoint"/"targetPoint") и ищем ближайшую фигуру (vertex)
    по расстоянию от точки до bounding box. При наложении нескольких
    фигур-кандидатов на одной позиции — приоритет отдаётся последней
    по порядку в XML (обычно то, что нарисовано позже, визуально
    наверху).

  ЭВРИСТИКА 2 — "прилипание" плавающих подписей к линии (для случая,
  когда автор диаграммы разместил рядом со стрелкой отдельную мелкую
  фигуру-подпись — например, "https, kafka (mtls)" или "NFS" в виде
  скруглённой "таблетки" — визуально рядом с линией, но НЕ как formal
  label этого edge и без всякой связи с другими фигурами). Такие
  фигуры никогда не выступают source/target ни одного ребра — они
  остаются полностью изолированными узлами графа. Скрипт находит все
  изолированные небольшие фигуры (площадь ниже --annotation-max-area)
  и, если фигура физически лежит близко (--annotation-threshold px)
  к линии (полилинии) какого-то ребра, "прикрепляет" её текст как
  дополнительную подпись этого ребра вместо того, чтобы рисовать её
  отдельным несвязанным узлом в mermaid.

Использование:
    python3 drawio_to_mermaid.py diagram.drawio
    python3 drawio_to_mermaid.py diagram.drawio --threshold 40
    python3 drawio_to_mermaid.py diagram.drawio --annotation-threshold 25
    python3 drawio_to_mermaid.py diagram.drawio --no-absorb-labels
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
    absorbed_into: str | None = None  # id ребра, к которому "прилипла" эта подпись
    absorbed_distance: float | None = None

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

    @property
    def area(self):
        return self.w * self.h

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
    waypoints: list = field(default_factory=list)  # промежуточные точки маршрута линии
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
    # Подписи, "прилипшие" к этому ребру эвристикой 2 (плавающие фигуры
    # рядом с линией). Каждый элемент: {"shape_id", "label", "distance"}.
    annotations: list = field(default_factory=list)


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
            waypoints = []

            if geom is not None:
                sp = geom.find("mxPoint[@as='sourcePoint']")
                tp = geom.find("mxPoint[@as='targetPoint']")
                if sp is not None:
                    source_point = (float(sp.get("x", 0)), float(sp.get("y", 0)))
                if tp is not None:
                    target_point = (float(tp.get("x", 0)), float(tp.get("y", 0)))

                # промежуточные точки маршрута (изгибы ортогональной линии) —
                # хранятся в <Array as="points"><mxPoint .../>...</Array>
                points_array = geom.find("Array[@as='points']")
                if points_array is not None:
                    for pt in points_array.findall("mxPoint"):
                        waypoints.append((float(pt.get("x", 0)), float(pt.get("y", 0))))

            edges.append(Edge(
                id=cell_id,
                label=value,
                source_id=source_id,
                target_id=target_id,
                source_point=source_point,
                target_point=target_point,
                waypoints=waypoints,
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
# Эвристика 2: "прилипание" плавающих подписей к линии ребра
# --------------------------------------------------------------------------

def point_to_segment_distance(px, py, x1, y1, x2, y2):
    """Расстояние от точки (px,py) до отрезка (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def build_edge_polyline(edge: "Edge", shapes: dict):
    """Собирает полилинию ребра: [начало] + waypoints + [конец].
    Если явных geometry-точек нет, использует центр резолвленной
    фигуры-эндпоинта как приближение. Возвращает список (x,y) точек
    либо [] если построить нечего (меньше 2 точек)."""
    points = []

    if edge.source_point is not None:
        points.append(edge.source_point)
    elif edge.resolved_source and edge.resolved_source in shapes:
        s = shapes[edge.resolved_source]
        points.append((s.cx, s.cy))

    points.extend(edge.waypoints)

    if edge.target_point is not None:
        points.append(edge.target_point)
    elif edge.resolved_target and edge.resolved_target in shapes:
        t = shapes[edge.resolved_target]
        points.append((t.cx, t.cy))

    return points if len(points) >= 2 else []


def distance_point_to_polyline(px, py, polyline):
    """Минимальное расстояние от точки до ближайшего отрезка полилинии."""
    best = float("inf")
    for i in range(len(polyline) - 1):
        x1, y1 = polyline[i]
        x2, y2 = polyline[i + 1]
        d = point_to_segment_distance(px, py, x1, y1, x2, y2)
        best = min(best, d)
    return best


def absorb_floating_labels(shapes: dict, edges: list, annotation_threshold: float,
                            annotation_max_area: float, debug: bool):
    """Изолированные небольшие фигуры (не участвующие ни в одном ребре
    как source/target), лежащие близко к линии какого-то ребра,
    трактуются как плавающая подпись этого ребра, а не отдельный узел
    графа. Типичный кейс: скруглённая "таблетка" с текстом протокола
    ("https, kafka (mtls)", "NFS"), просто визуально положенная рядом
    со стрелкой автором диаграммы, без formal-связи с чем-либо."""

    connected_ids = set()
    for e in edges:
        if e.resolved_source:
            connected_ids.add(e.resolved_source)
        if e.resolved_target:
            connected_ids.add(e.resolved_target)

    # предварительно считаем полилинии всех рёбер один раз
    edge_polylines = {e.id: build_edge_polyline(e, shapes) for e in edges}
    edges_by_id = {e.id: e for e in edges}

    debug_notes = []
    absorbed_count = 0

    for shape in shapes.values():
        if shape.id in connected_ids:
            continue  # это полноценный узел графа, не трогаем
        if shape.area > annotation_max_area:
            continue  # слишком большая фигура, чтобы быть просто подписью
        if not shape.label:
            continue  # нечего прикреплять — пустая подпись

        best_edge_id = None
        best_distance = float("inf")
        for edge_id, polyline in edge_polylines.items():
            if not polyline:
                continue
            d = distance_point_to_polyline(shape.cx, shape.cy, polyline)
            if d < best_distance:
                best_distance = d
                best_edge_id = edge_id

        if best_edge_id is not None and best_distance <= annotation_threshold:
            shape.absorbed_into = best_edge_id
            shape.absorbed_distance = round(best_distance, 1)
            edges_by_id[best_edge_id].annotations.append({
                "shape_id": shape.id,
                "label": shape.label,
                "distance": round(best_distance, 1),
            })
            absorbed_count += 1
            debug_notes.append(
                f"  '{shape.label}' (id={shape.id}) прилип к ребру "
                f"{best_edge_id} (дистанция {best_distance:.1f}px)"
            )

    if debug and debug_notes:
        print(f"--- Прилипание плавающих подписей ({absorbed_count}) ---", file=sys.stderr)
        for note in debug_notes:
            print(note, file=sys.stderr)


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
    # изолированные декоративные фигуры и "прилипшие" подписи пропускаем)
    for shape_id in used_shape_ids:
        shape = shapes[shape_id]
        label = shape.label.replace('"', "'") or shape.id
        lines.append(f'    {to_mermaid_id(shape_id)}["{label}"]')

    for edge in edges:
        if not (edge.resolved_source and edge.resolved_target):
            continue
        src = to_mermaid_id(edge.resolved_source)
        tgt = to_mermaid_id(edge.resolved_target)

        # объединяем formal label ребра с "прилипшими" плавающими подписями
        label_parts = []
        if edge.label:
            label_parts.append(edge.label)
        label_parts.extend(a["label"] for a in edge.annotations)
        full_label = ", ".join(label_parts)

        if full_label:
            safe_label = full_label.replace('"', "'")
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
        if e.annotations:
            item["annotations"] = e.annotations
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

    nodes_out = []
    for s in shapes.values():
        node = {"id": s.id, "label": s.label, "x": s.x, "y": s.y, "w": s.w, "h": s.h}
        if s.absorbed_into:
            node["absorbed_into_edge"] = s.absorbed_into
            node["absorbed_distance"] = s.absorbed_distance
        nodes_out.append(node)

    data = {"nodes": nodes_out, "edges": edges_out}
    return json.dumps(data, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("drawio_file", help="Путь к .drawio/.xml файлу")
    parser.add_argument("--threshold", type=float, default=30.0,
                         help="Радиус поиска ближайшей фигуры к концу несвязанной линии, px (по умолчанию 30)")
    parser.add_argument("--annotation-threshold", type=float, default=40.0,
                         help="Макс. расстояние от плавающей подписи до линии ребра, "
                              "чтобы считать её подписью этого ребра, px (по умолчанию 40)")
    parser.add_argument("--annotation-max-area", type=float, default=8000.0,
                         help="Макс. площадь (w*h) фигуры, чтобы считать её кандидатом "
                              "в плавающую подпись, а не полноценным узлом (по умолчанию 8000, "
                              "т.е. примерно 100x80px)")
    parser.add_argument("--no-absorb-labels", action="store_true",
                         help="Отключить эвристику 2 (прилипание плавающих подписей к линиям) — "
                              "изолированные мелкие фигуры останутся отдельными несвязанными узлами")
    parser.add_argument("--format", choices=["mermaid", "json"], default="mermaid",
                         help="Формат вывода (по умолчанию mermaid)")
    parser.add_argument("--debug", action="store_true",
                         help="Печатать в stderr эвристические решения и нерезолвленные рёбра")
    parser.add_argument("-o", "--output", help="Файл для записи результата (по умолчанию stdout)")
    args = parser.parse_args()

    root = load_xml_root(args.drawio_file)
    shapes, edges = parse_shapes_and_edges(root)
    unresolved = resolve_edges(shapes, edges, args.threshold, args.debug)

    if not args.no_absorb_labels:
        absorb_floating_labels(
            shapes, edges, args.annotation_threshold, args.annotation_max_area, args.debug)

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
