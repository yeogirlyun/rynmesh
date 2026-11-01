import test from "node:test";
import assert from "node:assert/strict";
import {setTimeout as delay} from "node:timers/promises";
import {ActiveDeadline} from "../../../docs/acceptance/ryn-agent-delivery/reference/zcode-adapter.mjs";

test("approval wait exceeds active deadline, execution resumes with same remaining time",async()=>{
  const abort=new AbortController(), timer=new ActiveDeadline(0.08,abort);
  try {
    await timer.ownerWait(async()=>{await delay(140);assert.equal(abort.signal.aborted,false);});
    assert(timer.excludedMs>=130);
    assert.equal(abort.signal.aborted,false);
    await delay(100);
    assert.equal(abort.signal.aborted,true);
  } finally {timer.close();}
});

test("late approval cannot revive cancellation",async()=>{
  const abort=new AbortController(), timer=new ActiveDeadline(1,abort);
  try {
    await assert.rejects(timer.ownerWait(async()=>{abort.abort();return {approved:true};}),/reference_cancelled/);
    assert.equal(abort.signal.aborted,true);
  } finally {timer.close();}
});

test("failed owner callback restores active timeout",async()=>{
  const abort=new AbortController(), timer=new ActiveDeadline(0.08,abort);
  try {
    await assert.rejects(timer.ownerWait(async()=>{await delay(140);throw new Error("owner failed");}),/owner failed/);
    assert.equal(abort.signal.aborted,false);
    await delay(100);
    assert.equal(abort.signal.aborted,true);
  } finally {timer.close();}
});

test("already expired time cannot be renewed by approval",async()=>{
  const abort=new AbortController();let now=0;
  const timer=new ActiveDeadline(1,abort,()=>now);
  try {
    now=1001;
    await assert.rejects(timer.ownerWait(async()=>assert.fail("expired callback invoked")),/reference_cancelled/);
    assert.equal(abort.signal.aborted,true);
  } finally {timer.close();}
});
