import test from "node:test";
import assert from "node:assert/strict";
import {streamRynResponse} from "../../../docs/acceptance/ryn-agent-delivery/reference/zcode-adapter.mjs";

test("bridge frames retain exact boundaries and final content is checked",async()=>{
  const events=[];
  const rpc=async(method,request,onEvent)=>{
    assert.equal(method,"model");assert.equal(request.stream,true);
    onEvent({type:"text_delta",text:"one"});onEvent({type:"text_delta",text:" two"});
    return {message:{role:"assistant",content:"one two"},usage:{input_tokens:4,output_tokens:2}};
  };
  for await(const event of streamRynResponse(rpc,{messages:[]},new Set())) events.push(event);
  assert.deepEqual(events.filter(e=>e.type==="text_delta"),[{type:"text_delta",text:"one"},{type:"text_delta",text:" two"}]);
  assert.equal(events.at(-1).usage.totalTokens,6);
});

test("completed prose without real frames never becomes invented deltas",async()=>{
  const events=[];
  await assert.rejects(async()=>{
    for await(const event of streamRynResponse(async()=>({message:{role:"assistant",content:"not streamed"}}),{messages:[]},new Set())) events.push(event);
  },/reference_stream_text_mismatch/);
  assert.equal(events.filter(e=>e.type==="text_delta").length,0);
});

test("parent abort ends an unresolved provider stream",async()=>{
  const abort=new AbortController();
  const stream=streamRynResponse(()=>new Promise(()=>{}),{messages:[],abortSignal:abort.signal},new Set());
  assert.equal((await stream.next()).value.type,"start");
  const next=stream.next();abort.abort();
  await assert.rejects(next,/reference_cancelled/);
});
