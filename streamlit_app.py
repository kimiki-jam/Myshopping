"""쇼핑 리스트 Streamlit 앱 (PRD.md 기준, 웹 버전 app.js를 Python으로 옮긴 것)

실행: streamlit run streamlit_app.py

구조
- Python: 데이터 저장(shopping_list.json), 파일 잠금, 손상 파일 백업, 여러 탭/사용자 간 동기화
- 화면(목록·입력창): Streamlit 커스텀 컴포넌트(components.v2) 하나가 브라우저에서 바로 그린다.
  Streamlit 위젯으로 행마다 그리면 동작할 때마다 서버 왕복 + 전체 다시 그리기가 필요해
  아이템 100개에서 0.7초 이상 걸리므로(PRD 기준 0.1초), 화면은 즉시 반영하고 저장만 Python이 맡는다.

컴포넌트 ↔ Python 통신
- 컴포넌트는 사용자 동작을 고유 opId가 붙은 작업(op) 목록으로 보낸다 (state 값 "ops").
- Python은 아직 처리하지 않은 작업만 파일에 반영하고, 처리한 opId 목록을 data로 돌려준다.
- 컴포넌트는 처리된 작업을 대기 목록에서 지운다. 연타해도 작업이 빠지거나 두 번 적용되지 않는다.
"""

import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# 데이터 저장 파일 경로
DATA_FILE = Path(__file__).with_name("shopping_list.json")
# 아이템 이름 최대 길이
MAX_NAME_LENGTH = 50
# 다른 탭/사용자의 변경을 화면에 반영하는 주기
SYNC_INTERVAL = "3s"
# 처리한 작업 id를 기억하는 개수
APPLIED_OPS_LIMIT = 300


@st.cache_resource
def get_file_lock() -> threading.RLock:
    """모든 세션이 공유하는 파일 잠금 (스크립트가 다시 실행돼도 같은 객체 유지)"""
    return threading.RLock()


# ===== 데이터 처리 =====

def normalize_name(value) -> str:
    """앞뒤 공백을 제거하고 최대 길이로 자른다."""
    return str(value).strip()[:MAX_NAME_LENGTH]


def is_valid_item(item) -> bool:
    """저장된 값이 올바른 아이템 형태인지 검사한다."""
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("name"), str)
        and item["name"].strip() != ""
        and isinstance(item.get("checked"), bool)
    )


class StorageError(Exception):
    """파일을 잠시 읽거나 쓸 수 없는 상태 (손상과 구분한다)"""


def retry_io(action, attempts: int = 20, delay: float = 0.05):
    """Windows에서 백신·검색 색인 등이 파일을 잠깐 잡고 있으면 PermissionError가 나므로 잠시 후 다시 시도한다."""
    for attempt in range(attempts):
        try:
            return action()
        except FileNotFoundError:
            raise
        except OSError as error:
            if attempt == attempts - 1:
                raise StorageError(str(error)) from error
            time.sleep(delay)


def log_error(message: str) -> None:
    print(f"[shopping_list] {message}", file=sys.stderr, flush=True)


def write_file(items: list[dict]) -> None:
    """임시 파일에 쓴 뒤 교체해서, 저장 도중 끊겨도 파일이 깨지지 않게 한다."""
    tmp = DATA_FILE.with_suffix(".json.tmp")
    data = json.dumps(items, ensure_ascii=False, indent=2)
    retry_io(lambda: tmp.write_text(data, encoding="utf-8"))
    retry_io(lambda: os.replace(tmp, DATA_FILE))


def backup_file() -> str:
    """손상된 원본 파일을 지우지 않고 백업해 둔다."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA_FILE.with_name(f"{DATA_FILE.stem}.backup-{stamp}.json")
    retry_io(lambda: shutil.copy2(DATA_FILE, backup))
    return backup.name


def warn(message: str) -> None:
    st.session_state.load_warning = message


def read_items_unlocked() -> list[dict]:
    """파일에서 아이템 목록을 읽는다. 잠금은 호출하는 쪽에서 잡는다.

    파일이 손상됐거나 잘못된 항목이 있으면 원본을 백업하고 정상 항목만 다시 저장한 뒤 경고를 남긴다.
    """
    try:
        raw = retry_io(DATA_FILE.read_bytes)
    except FileNotFoundError:
        return []
    # 읽기 자체가 실패한 경우(StorageError)는 손상이 아니므로 그대로 올려 보내 파일을 덮어쓰지 않는다
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None

    if not isinstance(parsed, list):
        backup = backup_file()
        write_file([])
        warn(f"저장 파일을 읽을 수 없어 빈 목록으로 시작합니다. 원본은 {backup}에 백업했습니다.")
        return []

    items, seen_ids = [], set()
    for item in parsed:
        # 형식이 잘못됐거나 id가 중복된 아이템은 제외
        if not is_valid_item(item) or item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        items.append({
            "id": item["id"],
            "name": normalize_name(item["name"]),
            "checked": item["checked"],
            "createdAt": item.get("createdAt") if isinstance(item.get("createdAt"), str)
            else datetime.now(timezone.utc).isoformat(),
        })

    if items != parsed:
        backup = backup_file()
        write_file(items)
        warn(f"저장 파일의 잘못된 항목을 정리했습니다. 원본은 {backup}에 백업했습니다.")
    return items


def load_items() -> list[dict]:
    """파일에서 최신 아이템 목록을 불러온다. 잠시 읽을 수 없으면 마지막으로 읽은 목록을 쓴다."""
    with get_file_lock():
        try:
            items = read_items_unlocked()
        except StorageError as error:
            log_error(f"읽기 실패: {error}")
            warn("저장 파일을 잠시 읽을 수 없어 마지막으로 불러온 목록을 표시합니다.")
            return st.session_state.get("last_items", [])
    st.session_state.last_items = items
    return items


def modify_items(change) -> bool:
    """파일의 최신 목록을 읽어 change(items)를 적용하고 저장한다. 저장에 성공하면 True.

    다른 탭의 변경을 덮어쓰지 않도록 읽기-수정-저장을 하나의 잠금 안에서 처리한다.
    """
    with get_file_lock():
        try:
            items = read_items_unlocked()
            change(items)
            write_file(items)
            return True
        except StorageError as error:
            log_error(f"저장 실패: {error}")
            warn("저장하지 못했습니다. 잠시 후 자동으로 다시 시도합니다.")
            return False


def find(items: list[dict], item_id):
    return next((item for item in items if item["id"] == item_id), None)


# ===== 컴포넌트 작업(op) 처리 =====

def apply_op(items: list[dict], op: dict, deleted: set) -> None:
    """작업 하나를 목록에 적용한다. 잘못된 작업은 무시한다. 삭제한 id는 deleted에 모은다."""
    op_type, item_id = op.get("type"), op.get("id")
    if not isinstance(item_id, str):
        return

    if op_type == "add":  # F-01
        name = normalize_name(op.get("name", ""))
        # 이미 삭제된 id의 add가 늦게 다시 적용되어 아이템이 되살아나는 것 방지
        if name and not find(items, item_id) and item_id not in st.session_state.deleted_ids:
            items.append({
                "id": item_id,
                "name": name,
                "checked": False,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
        return

    item = find(items, item_id)
    if op_type == "delete":  # F-03
        items[:] = [i for i in items if i["id"] != item_id]
        deleted.add(item_id)
    elif item is None:
        warn("다른 곳에서 이미 삭제된 아이템입니다.")
    elif op_type == "toggle" and isinstance(op.get("checked"), bool):  # F-04
        item["checked"] = op["checked"]
    elif op_type == "rename":  # F-02 (빈 값이면 기존 값 유지)
        name = normalize_name(op.get("name", ""))
        if name:
            item["name"] = name


def get_component_ops() -> list:
    state = st.session_state.get("shopping")
    if state is None:
        return []
    ops = state.get("ops") if hasattr(state, "get") else getattr(state, "ops", None)
    return ops if isinstance(ops, list) else []


def apply_ops() -> None:
    """컴포넌트가 보낸 작업 중 아직 처리하지 않은 것만 파일에 반영한다. (F-05)

    Streamlit은 실행이 중단되면 새 실행을 바로 시작하므로, 같은 세션의 콜백 두 개가 겹칠 수 있다.
    "처리 여부 확인 → 파일 반영 → 처리 기록"을 하나의 잠금 안에서 해야 작업이 두 번 적용되지 않는다.
    """
    st.session_state.setdefault("deleted_ids", set())
    with get_file_lock():
        applied = st.session_state.setdefault("applied_ops", [])
        applied_set = set(applied)
        new_ops = [
            op for op in get_component_ops()
            if isinstance(op, dict) and isinstance(op.get("opId"), str) and op["opId"] not in applied_set
        ]
        if not new_ops:
            return

        deleted: set = set()

        def change(items):
            deleted.clear()
            for op in new_ops:
                apply_op(items, op, deleted)

        # 저장에 실패하면 처리 기록을 남기지 않는다 → 컴포넌트가 작업을 계속 보관하고, 다음 동기화 때 다시 시도
        if not modify_items(change):
            return
        st.session_state.deleted_ids |= deleted
        applied.extend(op["opId"] for op in new_ops)
        del applied[:-APPLIED_OPS_LIMIT]


# ===== 화면 컴포넌트 (HTML/CSS/JS) =====

COMPONENT_CSS = """
:host {
  --color-primary: #2f7d5b;
  --color-primary-hover: #256649;
  --color-primary-soft: #e3f1ea;
  --color-surface: #ffffff;
  --color-surface-hover: #f6f8f6;
  --color-border: #dfe5e0;
  --color-text: #1e2a24;
  --color-text-muted: #5f6b64;
  --color-completed: #5f6b64;
  --color-danger: #b23a34;
  --color-danger-soft: #fbeceb;
  --control-height: 44px;
  display: block;
  font-family: inherit;
  color: var(--color-text);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
button, input { font: inherit; color: inherit; }
.visually-hidden {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
[hidden] { display: none !important; }
:focus-visible { outline: 3px solid var(--color-primary); outline-offset: 2px; }

.item-form { display: flex; gap: 8px; }
.item-input, .edit-input {
  min-width: 0; height: var(--control-height); padding: 0 14px;
  background: var(--color-surface); border: 1.5px solid var(--color-border); border-radius: 10px;
}
.item-input { flex: 1; }
.item-input::placeholder { color: #66736c; }
.item-input:focus, .edit-input:focus {
  outline: none; border-color: var(--color-primary); box-shadow: 0 0 0 3px var(--color-primary-soft);
}
.add-button {
  flex-shrink: 0; height: var(--control-height); padding: 0 20px; font-weight: 600;
  color: #fff; background: var(--color-primary); border: none; border-radius: 10px; cursor: pointer;
}
.add-button:hover { background: var(--color-primary-hover); }
.form-error { min-height: 1.4em; margin: 4px 2px 6px; font-size: 0.85rem; color: var(--color-danger); }

.item-list { list-style: none; }
.item {
  display: flex; align-items: center; gap: 8px; min-height: 56px; padding: 6px 8px;
  margin-bottom: 8px; background: var(--color-surface);
  border: 1px solid var(--color-border); border-radius: 10px;
}
.item:hover { background: var(--color-surface-hover); }
.item-checkbox {
  flex-shrink: 0; width: 22px; height: 22px; margin: 9px; cursor: pointer;
  accent-color: var(--color-primary);
}
.item-checkbox:disabled { cursor: not-allowed; opacity: 0.5; }
.item-name { flex: 1; min-width: 0; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.item.completed .item-name { color: var(--color-completed); text-decoration: line-through; }
.edit-input { flex: 1; height: 40px; padding: 0 10px; }
.item-actions { display: flex; flex-shrink: 0; gap: 4px; }
.item-button {
  min-width: 44px; min-height: 40px; padding: 0 10px; font-size: 0.875rem; font-weight: 500;
  color: #4d5852; background: transparent; border: 1px solid var(--color-border);
  border-radius: 8px; cursor: pointer;
}
.item-button:hover { color: var(--color-text); background: var(--color-border); }
.delete-button { color: var(--color-danger); }
.delete-button:hover { color: var(--color-danger); background: var(--color-danger-soft); }
.save-button { color: #fff; background: var(--color-primary); border-color: var(--color-primary); }
.save-button:hover { color: #fff; background: var(--color-primary-hover); }

.empty-message { padding: 32px 16px; text-align: center; color: #66736c; }
.item-count { margin-top: 8px; font-size: 0.85rem; text-align: center; color: #5f6b64; }

@media (max-width: 480px) {
  .add-button { padding: 0 16px; }
  .item { gap: 4px; padding: 6px 4px; }
  .item-checkbox { margin: 9px 7px; }
  .item-button { padding: 0 8px; }
}
"""

COMPONENT_JS = r"""
// 컴포넌트 인스턴스별 상태 (Python이 다시 실행돼도 유지)
const STORE = new WeakMap();

const createId = () =>
  (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`);

function createApp(root) {
  const app = document.createElement("div");
  app.className = "shopping-app";
  app.innerHTML = `
    <form class="item-form" autocomplete="off">
      <label for="item-input" class="visually-hidden">살 물건</label>
      <input type="text" id="item-input" class="item-input" placeholder="살 물건을 입력하세요..." maxlength="50">
      <button type="submit" class="add-button">추가</button>
    </form>
    <p class="form-error" role="alert"></p>
    <ul class="item-list"></ul>
    <p class="empty-message">쇼핑 리스트가 비어 있어요. 살 물건을 추가해 보세요!</p>
    <p class="item-count" aria-live="polite"></p>`;
  root.appendChild(app);

  const form = app.querySelector(".item-form");
  const input = app.querySelector(".item-input");
  const formError = app.querySelector(".form-error");
  const list = app.querySelector(".item-list");
  const emptyMessage = app.querySelector(".empty-message");
  const itemCount = app.querySelector(".item-count");

  const s = {
    serverItems: [],   // Python(파일)이 알려준 목록
    pending: [],       // 아직 Python이 처리하지 않은 작업
    editingId: null,   // 편집 중인 아이템 (한 번에 하나)
    editDraft: "",     // 편집 입력값
    maxLength: 50,
    setStateValue: null,
  };

  const normalize = (v) => String(v).trim().slice(0, s.maxLength);

  // 서버 목록 위에 대기 중인 작업을 적용한 화면용 목록 (낙관적 반영)
  const view = () => {
    const items = s.serverItems.map((i) => ({ ...i }));
    for (const op of s.pending) {
      const idx = items.findIndex((i) => i.id === op.id);
      if (op.type === "add" && idx < 0) items.push({ id: op.id, name: op.name, checked: false });
      else if (idx < 0) continue;
      else if (op.type === "toggle") items[idx].checked = op.checked;
      else if (op.type === "rename") items[idx].name = op.name;
      else if (op.type === "delete") items.splice(idx, 1);
    }
    return items;
  };

  const makeButton = (text, action, label) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `item-button ${action}-button`;
    b.dataset.action = action;
    b.textContent = text;
    b.setAttribute("aria-label", label);
    return b;
  };

  // 사용자 입력은 textContent / value로만 넣는다 (XSS 방지)
  const createItem = (item, mode) => {
    const li = document.createElement("li");
    li.className = "item";
    li.dataset.id = item.id;
    li.dataset.mode = mode;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "item-checkbox";
    const actions = document.createElement("div");
    actions.className = "item-actions";
    if (mode === "edit") {
      li.classList.add("editing");
      checkbox.disabled = true;
      const edit = document.createElement("input");
      edit.type = "text";
      edit.className = "edit-input";
      edit.maxLength = s.maxLength;
      edit.value = s.editDraft;
      edit.setAttribute("aria-label", `${item.name} 이름 수정`);
      actions.append(makeButton("저장", "save", `${item.name} 수정 저장`), makeButton("취소", "cancel", `${item.name} 수정 취소`));
      li.append(checkbox, edit, actions);
    } else {
      const name = document.createElement("span");
      name.className = "item-name";
      actions.append(makeButton("수정", "edit", ""), makeButton("삭제", "delete", ""));
      li.append(checkbox, name, actions);
    }
    updateItem(li, item);
    return li;
  };

  const updateItem = (li, item) => {
    const checkbox = li.querySelector(".item-checkbox");
    if (checkbox.checked !== item.checked) checkbox.checked = item.checked;
    checkbox.setAttribute("aria-label", `${item.name} 구매 완료`);
    li.classList.toggle("completed", item.checked);
    if (li.dataset.mode === "view") {
      const name = li.querySelector(".item-name");
      if (name.textContent !== item.name) name.textContent = item.name;
      li.querySelector(".edit-button").setAttribute("aria-label", `${item.name} 수정`);
      li.querySelector(".delete-button").setAttribute("aria-label", `${item.name} 삭제`);
    }
  };

  // 바뀐 행만 갱신한다 (포커스와 편집 중 입력값 유지)
  const render = () => {
    const items = view();
    if (s.editingId && !items.some((i) => i.id === s.editingId)) s.editingId = null;
    const existing = new Map([...list.children].map((li) => [li.dataset.id, li]));
    items.forEach((item, index) => {
      const mode = item.id === s.editingId ? "edit" : "view";
      let li = existing.get(item.id);
      existing.delete(item.id);
      if (!li || li.dataset.mode !== mode) {
        const fresh = createItem(item, mode);
        if (li) li.replaceWith(fresh);
        li = fresh;
      } else {
        updateItem(li, item);
      }
      if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
    });
    existing.forEach((li) => li.remove());

    emptyMessage.hidden = items.length > 0;
    const done = items.filter((i) => i.checked).length;
    itemCount.textContent = `전체 ${items.length}개 · 완료 ${done}개`;
  };

  const send = () => {
    if (s.setStateValue) s.setStateValue("ops", s.pending.map((op) => ({ ...op })));
  };

  const pushOp = (op) => {
    s.pending.push({ opId: createId(), ...op });
    render();
    send();
  };

  const focusLater = (selector) => {
    const el = selector && list.querySelector(selector);
    (el || input).focus();
  };

  const startEdit = (id) => {
    const item = view().find((i) => i.id === id);
    if (!item) return;
    s.editingId = id;
    s.editDraft = item.name;
    render();
    const edit = list.querySelector(".edit-input");
    if (edit) { edit.focus(); edit.setSelectionRange(edit.value.length, edit.value.length); }
  };

  const finishEdit = (id, save) => {
    const item = view().find((i) => i.id === id);
    s.editingId = null;
    const name = normalize(s.editDraft);
    if (save && item && name && name !== item.name) pushOp({ type: "rename", id, name });
    else render();
    focusLater(`li[data-id="${CSS.escape(id)}"] .edit-button`);
  };

  const deleteItem = (id) => {
    const items = view();
    const index = items.findIndex((i) => i.id === id);
    if (index < 0) return;
    if (!window.confirm(`'${items[index].name}'을(를) 삭제할까요?`)) return;
    // 삭제 후 포커스: 다음 아이템 → 이전 아이템 → 입력창
    const neighbor = items[index + 1] || items[index - 1];
    pushOp({ type: "delete", id });
    focusLater(neighbor && `li[data-id="${CSS.escape(neighbor.id)}"] .item-checkbox`);
  };

  // ===== 이벤트 =====
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = normalize(input.value);
    input.value = "";
    input.focus();
    if (!name) { formError.textContent = "살 물건 이름을 입력해 주세요."; return; }
    formError.textContent = "";
    pushOp({ type: "add", id: createId(), name });
  });

  list.addEventListener("change", (e) => {
    if (!e.target.matches(".item-checkbox")) return;
    const li = e.target.closest("li");
    pushOp({ type: "toggle", id: li.dataset.id, checked: e.target.checked });
  });

  list.addEventListener("click", (e) => {
    const button = e.target.closest("button[data-action]");
    if (!button) return;
    const id = button.closest("li").dataset.id;
    const action = button.dataset.action;
    // [수정] 더블클릭 시 두 번째 클릭이 같은 자리의 [저장]을 누르지 않도록 무시
    if ((action === "save" || action === "cancel") && e.detail > 1) return;
    if (action === "edit") startEdit(id);
    else if (action === "delete") deleteItem(id);
    else if (action === "save") finishEdit(id, true);
    else if (action === "cancel") finishEdit(id, false);
  });

  list.addEventListener("dblclick", (e) => {
    const name = e.target.closest(".item-name");
    if (name) startEdit(name.closest("li").dataset.id);
  });

  list.addEventListener("input", (e) => {
    if (e.target.matches(".edit-input")) s.editDraft = e.target.value;
  });

  list.addEventListener("keydown", (e) => {
    if (!e.target.matches(".edit-input")) return;
    if (e.isComposing || e.keyCode === 229) return; // 한글 조합 중 Enter/Esc 무시
    const id = e.target.closest("li").dataset.id;
    if (e.key === "Enter") { e.preventDefault(); finishEdit(id, true); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finishEdit(id, false); }
  });

  // Python에서 새 데이터가 오면 처리된 작업을 대기 목록에서 지우고 다시 그린다
  s.onData = (data) => {
    s.serverItems = Array.isArray(data?.items) ? data.items : [];
    s.maxLength = data?.maxLength || 50;
    input.maxLength = s.maxLength;
    const applied = new Set(data?.applied || []);
    s.pending = s.pending.filter((op) => !applied.has(op.opId));
    render();
  };
  return s;
}

export default function (component) {
  const { data, setStateValue, parentElement } = component;
  document.documentElement.lang = "ko";
  let s = STORE.get(parentElement);
  if (!s) {
    s = createApp(parentElement);
    STORE.set(parentElement, s);
  }
  s.setStateValue = setStateValue;
  s.onData(data);
}
"""

shopping_list = st.components.v2.component(
    "shopping_list",
    css=COMPONENT_CSS,
    js=COMPONENT_JS,
)


# ===== 화면 =====

st.set_page_config(page_title="나의 쇼핑 리스트", page_icon="🛒", layout="centered")

st.session_state.setdefault("applied_ops", [])
st.session_state.setdefault("deleted_ids", set())
st.session_state.setdefault("load_warning", "")

st.markdown(
    """
    <style>
    .block-container { max-width: 560px; padding-top: 2.5rem; }
    h1.app-title { text-align: center; font-size: clamp(1.6rem, 7vw, 2.5rem); white-space: nowrap; }
    </style>
    <h1 class='app-title'>🛒 나의 쇼핑 리스트</h1>
    """,
    unsafe_allow_html=True,
)


@st.fragment(run_every=SYNC_INTERVAL)
def shopping_list_section() -> None:
    """목록 영역. 주기적으로 파일을 다시 읽어 다른 탭/사용자의 변경을 반영한다."""
    # 저장에 실패해 남아 있는 작업이 있으면 다시 시도
    apply_ops()
    items = load_items()
    if st.session_state.load_warning:
        st.warning(st.session_state.load_warning)
        st.session_state.load_warning = ""
    shopping_list(
        key="shopping",
        data={
            "items": items,
            "applied": st.session_state.applied_ops[-APPLIED_OPS_LIMIT:],
            "maxLength": MAX_NAME_LENGTH,
        },
        default={"ops": []},
        on_ops_change=apply_ops,
    )


shopping_list_section()
