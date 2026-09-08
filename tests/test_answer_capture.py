from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from neko_answer_probe import _CAPTURE_SCRIPT, _answer_summary, _verified_tool_names


def capture_scenario(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    harness = r"""
const listeners = {};
let clock = 1000;
let serial = 0;
const tasks = new Map();
const bubbles = [];
Date.now = () => clock;
function later(callback, delay) {
  const id = ++serial;
  tasks.set(id, {at: clock + delay, callback});
  return id;
}
function advance(duration) {
  const until = clock + duration;
  for (let count = 0; count < 100000; count++) {
    const next = [...tasks.entries()].sort((a,b) => a[1].at-b[1].at)[0];
    if (!next || next[1].at > until) { clock = until; return; }
    clock = next[1].at;
    tasks.delete(next[0]);
    next[1].callback();
  }
  throw new Error('fake clock runaway');
}
global.window = {
  addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
  requestAnimationFrame: fn => later(fn, 16),
  setTimeout: later,
  clearTimeout: id => tasks.delete(id),
  getComputedStyle: () => ({display:'block',visibility:'visible'}),
  _realisticGeminiQueue: [],
  _realisticGeminiBuffer: '',
  _isProcessingRealisticQueue: false,
  _realisticProcessingOwner: null,
};
function emit(name, detail) { for (const fn of listeners[name] || []) fn({detail}); }
function bubble(id, text, turnId='turn-a') {
  const block = {isConnected:true,offsetWidth:100,offsetHeight:20,
    innerText:text,textContent:text,getClientRects:()=>[{}]};
  bubbles.push({...block, dataset:{messageId:id,messageRole:'assistant',messageStatus:'streaming',turnId},
    matches:s=>s==='[data-message-role="assistant"]',querySelectorAll:()=>[block]});
}
global.document = {
  documentElement: {},
  querySelectorAll: selector => selector.includes('data-message') ? bubbles : [],
};
global.MutationObserver = class { constructor(fn) {this.fn=fn;} observe() {} disconnect() {} };
window.reactChatWindowHost = {getState:()=>({messages:bubbles.map(b=>({
  id:b.dataset.messageId,role:'assistant',status:'streaming',text:b.innerText,turnId:b.dataset.turnId,
  blocks:[{type:'text',text:b.innerText}],
}))})};
(__CAPTURE__)();
const current = {
  armed:true,question:'synthetic query',requestId:'request-a',submittedAt:1000,
  modernIds:[],legacyCount:0,baselines:{},starts:[],ends:[],turns:[],signals:[],
};
window.__hearthstoneAnswerProbe.current=current;
function start(turnId='turn-a',requestId='request-a') {
  emit('neko-assistant-turn-start',{turnId,requestId,source:'gemini_response_first_chunk'});
}
function end(turnId='turn-a',requestId='request-a') {
  emit('neko-assistant-turn-end',{turnId,requestId,source:'turn_end'});
}
__SCENARIO__
process.stdout.write(JSON.stringify({turns:current.turns,bubbleCount:bubbles.length}));
"""
    script = harness.replace("__CAPTURE__", _CAPTURE_SCRIPT).replace("__SCENARIO__", scenario)
    result = subprocess.run(
        [node, "-e", script], check=True, capture_output=True,
        encoding="utf-8", timeout=10,
    )
    return json.loads(result.stdout)


def test_capture_waits_for_delayed_fact_bubbles() -> None:
    result = capture_scenario("""
start();
bubble('one','Checking.');
window._realisticGeminiQueue=[{text:'Gold: 5.',turnId:'turn-a'}];
window._isProcessingRealisticQueue=true;
end();
advance(2100);
bubble('two','Gold: 5.');
window._realisticGeminiQueue=[];
advance(2100);
bubble('three','Upgrade costs 6; cannot upgrade.');
window._isProcessingRealisticQueue=false;
advance(2500);
""")
    turn = result["turns"][0]
    assert turn["settled"] is True
    assert turn["answer"] == "Checking.\nGold: 5.\nUpgrade costs 6; cannot upgrade."
    assert turn["bubbleCount"] == 3


def test_capture_does_not_finish_while_popped_item_is_processing() -> None:
    result = capture_scenario("""
start(); bubble('one','Checking.');
window._isProcessingRealisticQueue=true;
window._realisticProcessingOwner={};
end(); advance(2200);
""")
    assert result["turns"][0]["settled"] is False


def test_capture_timeout_does_not_accept_partial_text() -> None:
    result = capture_scenario("""
start(); bubble('one','Checking.');
window._realisticGeminiQueue=[{text:'Never delivered',turnId:'turn-a'}];
window._isProcessingRealisticQueue=true;
end(); advance(65000);
""")
    turn = result["turns"][0]
    assert turn["settled"] is False
    assert turn["settleFailure"]


def test_capture_does_not_wait_on_historical_streaming_status() -> None:
    result = capture_scenario("""
start(); bubble('one','Round 11.'); end(); advance(2500);
""")
    assert result["turns"][0]["settled"] is True
    assert result["turns"][0]["answer"] == "Round 11."


def test_capture_does_not_merge_an_unrelated_turn() -> None:
    result = capture_scenario("""
start(); bubble('one','Checking.');
window._isProcessingRealisticQueue=true;
end(); advance(200);
start('turn-b','request-b');
bubble('foreign','Unrelated private conversation.','turn-b');
end('turn-b','request-b');
window._isProcessingRealisticQueue=false;
advance(2500);
""")
    turn = result["turns"][0]
    assert not turn["settled"] or "Unrelated" not in turn["answer"]


def test_capture_includes_later_correction_before_queue_drains() -> None:
    result = capture_scenario("""
start(); bubble('one','Can upgrade.');
window._isProcessingRealisticQueue=true;
window._realisticGeminiQueue=[{text:'Correction',turnId:'turn-a'}];
end(); advance(2100);
bubble('two','Correction: cannot upgrade; missing one gold.');
window._realisticGeminiQueue=[];
window._isProcessingRealisticQueue=false;
advance(2500);
""")
    turn = result["turns"][0]
    assert turn["settled"] is True
    assert turn["answer"] == "Can upgrade.\nCorrection: cannot upgrade; missing one gold."


@pytest.mark.parametrize("late_change", [
    "window._realisticGeminiQueue=[{text:'Late',turnId:'turn-a'}];",
    "bubble('two','Late fact.');",
])
def test_capture_rechecks_display_at_deadline(late_change) -> None:
    result = capture_scenario("""
start(); bubble('one','First.'); end();
advance(59999);
""" + late_change + "advance(1);")
    assert result["turns"][0]["settled"] is False
    assert result["turns"][0]["settleFailure"]


def test_unverified_answer_does_not_erase_verified_callback() -> None:
    digest = "a" * 64
    call = {
        "name": "hearthstone_live_state", "status": "completed", "is_error": False,
        "output_contract": {"fact_sha256": digest},
    }
    assert _verified_tool_names([call], digest) == ["hearthstone_live_state"]
    summary = _answer_summary(None, None, submitted_at_ms=1000, salt=b"test")
    assert summary["status"] == "UNVERIFIED"
    assert "passed_fact_count" not in summary


@pytest.mark.parametrize("override", [
    {"status": "started"}, {"is_error": True}, {"output_contract": {"fact_sha256": "b" * 64}},
])
def test_callback_observation_requires_completed_matching_facts(override) -> None:
    digest = "a" * 64
    call = {
        "name": "hearthstone_live_state", "status": "completed", "is_error": False,
        "output_contract": {"fact_sha256": digest}, **override,
    }
    assert _verified_tool_names([call], digest) == []
