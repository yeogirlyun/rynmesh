import assert from "node:assert/strict";

// Acceptance-only active deadline. Upstream planning/execution stays unchanged.
export class ActiveDeadline {
  constructor(seconds, abort, clock=()=>performance.now()) {
    this.clock=clock; this.abort=abort; this.remaining=seconds*1000;
    this.waiting=false; this.excludedMs=0; this.closed=false; this.arm();
  }
  arm() {
    this.started=this.clock();
    this.timer=setTimeout(()=>this.abort.abort(),Math.max(0,this.remaining));
  }
  async ownerWait(invoke) {
    if(this.closed || this.waiting || this.abort.signal.aborted) throw new Error("reference_cancelled");
    this.remaining-=this.clock()-this.started;
    clearTimeout(this.timer);
    if(this.remaining<=0) {this.abort.abort();throw new Error("reference_cancelled");}
    this.waiting=true;
    const start=this.clock();
    try {
      const result=await invoke();
      if(this.closed || this.abort.signal.aborted) throw new Error("reference_cancelled");
      return result;
    }
    finally {
      this.excludedMs+=this.clock()-start;
      this.waiting=false;
      if(!this.closed) this.arm();
    }
  }
  close() {this.closed=true;clearTimeout(this.timer);}
}

// Acceptance adapter around the pinned upstream public API. No upstream source edits.
// The host RPC owns credentials, exact Ryn approvals, resource scope and budgets.
export function toRynRequest(request, allowedNames) {
  if (request.responseJsonSchema) throw new Error("reference_json_schema_not_supported");
  const messages = request.messages.map(item => {
    let content = item.content;
    let reasoning;
    if (Array.isArray(content)) {
      if (content.some(block => !["text", "reasoning"].includes(block.type))) {
        throw new Error("reference_nontext_input_not_supported");
      }
      reasoning = content.filter(block => block.type === "reasoning").map(block => block.text).join("\n");
      content = content.filter(block => block.type === "text").map(block => block.text).join("\n\n");
    }
    if (typeof content !== "string") throw new Error("reference_invalid_content");
    const message = {role:item.role, content};
    if (reasoning) {
      if (item.role !== "assistant") throw new Error("reference_invalid_reasoning_role");
      message.reasoning_content = reasoning;
    }
    if (item.toolCalls?.length) {
      message.tool_calls = item.toolCalls.map(call => {
        if (call.providerExecuted || !allowedNames.has(call.name)) throw new Error("reference_unknown_tool");
        return {id:call.id, type:"function", function:{name:call.name, arguments:JSON.stringify(call.input)}};
      });
    }
    if (item.toolCallId) message.tool_call_id = item.toolCallId;
    return message;
  });
  const tools = (request.tools || []).map(tool => {
    if (!allowedNames.has(tool.name) || tool.providerNative) throw new Error("reference_unknown_tool");
    return {type:"function", function:{name:tool.name, description:tool.description || "", parameters:tool.inputSchema}};
  });
  return {messages, tools, max_output_tokens:request.options?.maxOutputTokens};
}

export function fromRynResponse(response, allowedNames) {
  const message = response.message;
  if (!message || message.role !== "assistant") throw new Error("reference_invalid_response");
  const toolCalls = (message.tool_calls || []).map(call => {
    if (!allowedNames.has(call.function?.name)) throw new Error("reference_unknown_tool");
    return {id:call.id, name:call.function.name, input:JSON.parse(call.function.arguments)};
  });
  const usage = {};
  if (Number.isInteger(response.usage?.input_tokens) && response.usage.input_tokens >= 0) usage.inputTokens = response.usage.input_tokens;
  if (Number.isInteger(response.usage?.output_tokens) && response.usage.output_tokens >= 0) usage.outputTokens = response.usage.output_tokens;
  if (usage.inputTokens !== undefined && usage.outputTokens !== undefined) usage.totalTokens = usage.inputTokens + usage.outputTokens;
  return {
    text:message.content || "", finishReason:toolCalls.length ? "tool-calls" : "stop", toolCalls, usage,
    ...(message.reasoning_content ? {reasoning:[{type:"reasoning", text:message.reasoning_content}]} : {}),
  };
}

// Deltas are transported as they arrive from the existing production bridge.
// Final content is checked, never split into invented streaming events.
export async function* streamRynResponse(rpc, request, names) {
  if(request.abortSignal?.aborted) throw new Error("reference_cancelled");
  const queue=[];
  let wake,done=false,failure,result,closed=false,text="";
  const notify=()=>{if(wake) {wake();wake=undefined;}};
  const aborted=()=>{failure=new Error("reference_cancelled");done=true;notify();};
  request.abortSignal?.addEventListener("abort",aborted,{once:true});
  const pending=rpc("model",{...toRynRequest(request,names),stream:true},event=>{
    if(!closed) {queue.push(event);notify();}
  }).then(value=>{result=value;done=true;notify();},error=>{failure=error;done=true;notify();});
  try {
    yield {type:"start"};
    while(!done || queue.length) {
      if(request.abortSignal?.aborted) throw new Error("reference_cancelled");
      while(queue.length) {const event=queue.shift();text+=event.text;yield event;}
      if(!done) await new Promise(resolve=>{wake=resolve;});
    }
    if(failure) throw failure;
    await pending;
    const final=fromRynResponse(result,names);
    assert.equal(text,final.text,"reference_stream_text_mismatch");
    for(const item of final.reasoning || []) yield {type:"reasoning_delta",text:item.text};
    for(const toolCall of final.toolCalls) yield {type:"tool_call",toolCall};
    yield {type:"finish",finishReason:final.finishReason,usage:final.usage};
  } finally {closed=true;request.abortSignal?.removeEventListener("abort",aborted);}
}

export async function runReference(core, contracts, rpc, config, storage) {
  const {AgentRuntime, createToolRegistry, createToolExecutor, PermissionService, defaultPermissionConfig} = core;
  const {InMemorySessionEventStore, createRootTraceContext} = contracts;
  const sessionId = config.session_id;
  const traceContext = createRootTraceContext({sessionId});
  const eventStore = new InMemorySessionEventStore({retention:"unbounded"});
  let sessionStore;
  if (config.session_db_path) {
    assert(storage?.createSqliteSessionStore, "reference_storage_required");
    sessionStore = storage.createSqliteSessionStore({dbPath:config.session_db_path});
    if (config.resume) {
      assert(await sessionStore.getSession(sessionId), "reference_session_missing");
      assert(await sessionStore.readTarget({sessionID:sessionId}), "reference_target_missing");
    } else {
      assert(!(await sessionStore.getSession(sessionId)), "reference_session_exists");
      await sessionStore.createSession({id:sessionId,projectID:"ryn-acceptance",slug:sessionId,
        directory:config.working_directory || "D:/ryn-reference-fixture",title:"Ryn acceptance",version:"3.14.3"});
      assert(await sessionStore.createTarget({sessionID:sessionId,objective:config.prompt}));
    }
  }
  const registry = createToolRegistry();
  const names = new Set(config.tools.map(spec => spec.function.name));
  assert.equal(names.size, config.tools.length);
  const permissionService = new PermissionService({...defaultPermissionConfig, allowedTools:new Set(), disallowedTools:new Set()});
  let executionTimer;
  const pendingPermissions = new Map();
  const permissionBroker = {listPendingRequests:()=>[...pendingPermissions.values()],requestPermission:async request => {
    if (!names.has(request.toolName)) return {decision:"deny", reason:"Tool not part of Ryn fixture"};
    pendingPermissions.set(request.toolCallId,request);
    try {
      const result = await executionTimer.ownerWait(()=>rpc("approval", {name:request.toolName, arguments:request.input, tool_call_id:request.toolCallId}));
      return {decision:result.approved === true ? "allow" : "deny", reason:result.reason || "Exact Ryn host policy"};
    } finally {pendingPermissions.delete(request.toolCallId);}
  }};
  for (const spec of config.tools) {
    const name = spec.function.name;
    const metadata = config.tool_metadata[name];
    const readOnly = metadata.effect === "read";
    const ownerInput = name === "ryn_request_input" && metadata.owner_input === true;
    registry.register({
      // The public upstream timeout policy retains parent abort handling. This
      // pure owner form consumes no active/model/tool execution time.
      ...(ownerInput ? {timeout:{kind:"none"}} : {}),
      metadata:{name, description:spec.function.description, readOnly, destructive:false,
        concurrentSafe:false, sideEffectScope:readOnly ? "none" : "network",
        riskLevel:readOnly ? "low" : "medium", needsApproval:!readOnly,
        maxOutputBytes:256*1024, timeoutMs:125000},
      inputSchema:spec.function.parameters,
      handler:async (input, context) => {
        if (context.abortSignal.aborted) throw new Error("reference_cancelled");
        const invoke=()=>rpc("tool", {name, arguments:input, tool_call_id:context.toolCallId});
        return ownerInput ? executionTimer.ownerWait(invoke) : invoke();
      },
    });
  }
  let runtime;
  const executor = createToolExecutor({registry, permissionService, permissionBroker, sessionId, traceContext,
    emitEvent:async event => { await eventStore.append(event); }});
  const properties = {
    contextWindow:config.context_window,
    inputFormat:{supportsText:true,supportsImage:false,supportsVideo:false,supportsAudio:false,supportsPdf:false},
    outputFormat:{supportsText:true},supportsToolCall:true,supportsJsonSchemaOutput:false,
    supportsNativeWebSearch:false,supportsMidConversationSystem:true,requiresMfjsToolSchema:false,
  };
  let querySource;
  const modelFactory = ({selection}) => {
    assert.equal(selection.providerId, config.provider_id);
    assert.equal(selection.modelId, config.model_id);
    const createModel = (options={}) => ({
      providerId:config.provider_id,modelId:config.model_id,displayName:config.model_id,properties,
      optionSpecs:{reasoningLevel:{values:["default"],map:"({})"},maxOutputTokens:{max:config.max_output_tokens ?? 4096,map:"({max_tokens:maxOutputTokens})"}},
      options:{maxOutputTokens:config.max_output_tokens ?? 4096,...options},
      bind(next={}) { return createModel({...this.options,...next}); },
      async generateText(request) {
        if (request.abortSignal?.aborted) throw new Error("reference_cancelled");
        assert.deepEqual(new Set(registry.list()), names);
        return fromRynResponse(await rpc("model", {...toRynRequest(request,names),
          ...(querySource ? {query_source:querySource} : {})}),names);
      },
      streamText(request) {
        if(!config.streaming) throw new Error("reference_streaming_disabled");
        // The existing production bridge streams tool-enabled requests only.
        // Reject before any dispatch so upstream compact's public non-stream
        // fallback can run once; never invent deltas from a completed summary.
        if(!request.tools?.length) throw new Error("reference_stream_transport_unavailable");
        return streamRynResponse(rpc,request,names);
      },
    });
    return createModel();
  };
  runtime = new AgentRuntime(sessionId, {
    mode:"build",presentationSurface:"terminal",modelStreaming:config.streaming ? "on" : "off",streamingToolExecution:"off",
    modelSelection:{providerId:config.provider_id,modelId:config.model_id},
    workingDirectory:config.working_directory || "D:/ryn-reference-fixture",
    toolAllowlist:[...names],toolConcurrency:{maxConcurrency:1},maxTurns:config.max_model_calls,
    dynamicWorkflowEnabled:false,subagents:{enabled:false},mcp:{enabled:false},
    memory:{enabled:false,extractionEnabled:false,use:false},
    runtimeFeatures:{nodeRepl:false,browserUse:false,computerUse:false},
    titleGeneration:{enabled:false},targetCompletionVerification:{enabled:!!sessionStore},
    ...(config.compact_policy ? {compact:config.compact_policy} : {}),
  },{eventStore,sessionStore,modelFactory,toolRegistry:registry,toolExecutor:executor,permissionService,permissionBroker,traceContext});
  assert.deepEqual(new Set(registry.list()),names);
  const abort = new AbortController();
  executionTimer = new ActiveDeadline(config.max_seconds,abort);
  let ownerControl;
  const applyControl=action=>{
    const stopped=runtime.stopActiveForegroundExecution({reason:`owner_${action}`});
    ownerControl={action,result:stopped};
  };
  rpc.setControlHandler?.(applyControl);
  if(config.observe_compaction) {
    const append=eventStore.append.bind(eventStore);
    eventStore.append=async event=>{
      const saved=await append(event);
      if(event.type===contracts.SessionEventType.ModelRequest) querySource=event.payload.querySource;
      if(event.type===contracts.SessionEventType.CompactCompleted && event.payload.status==="completed"
          && event.payload.boundaryId && event.payload.summaryMessageId) {
        const p=event.payload;
        const response=await rpc("checkpoint",{type:"compact_completed",session_id:sessionId,
          sequence_number:saved.sequenceNumber,boundary_id:p.boundaryId,summary_message_id:p.summaryMessageId,
          pre_compact_tokens:p.preCompactTokenCount,post_compact_tokens:p.truePostCompactTokenCount});
        if(response.action) applyControl(response.action);
      }
      return saved;
    };
  }
  try {
    let resumeEvidence;
    if (config.resume) {
      assert(sessionStore, "reference_storage_required");
      const before = await sessionStore.messages({sessionID:sessionId});
      const hydration = await runtime.resumeFromStore({abortSignal:abort.signal,traceContext});
      const after = await sessionStore.messages({sessionID:sessionId});
      // Cold resume hydrates history. It does not recreate an executor promise,
      // approve an interrupted call, synthesize success, or replay a mutation.
      resumeEvidence={resumed:true,session_id:sessionId,hydration,
        persisted_history_unchanged:JSON.stringify(before)===JSON.stringify(after),
        persisted_tools:after.flatMap(m=>m.parts.filter(p=>p.type==="tool").map(p=>
          ({call_id:p.callID,tool:p.tool,status:p.state.status,input:p.state.input}))),
        target_status:(await sessionStore.readTarget({sessionID:sessionId}))?.status,
        native_pending_approvals:runtime.getPendingPermissionRequests().length,
        limitation:"native_cold_resume_hydrates_interrupted_tools_without_resuming_execution",
        persistent_session:true,extra_host_tools:false,real_model:config.real_model===true};
      if(!config.resume_action) return resumeEvidence;
      assert(["owner_turn","continue"].includes(config.resume_action),"reference_invalid_resume_action");
      if(config.resume_action==="owner_turn") assert(typeof config.owner_message==="string" && config.owner_message.trim(),"reference_owner_message_required");
    }
    let result;
    try {
      if(config.resume_action==="continue") {
        // Only the collector's explicit original owner-resume intervention may
        // activate the same goal. This invokes the native planner, never tools.
        await sessionStore.updateTargetStatus({sessionID:sessionId,status:"active"});
        result=await runtime.continueActiveTargetLoop({trigger:"manual",verifyBeforeFirstContinue:false,
          abortSignal:abort.signal,traceContext});
      } else result = await runtime.executeTurn(config.resume_action==="owner_turn" ? config.owner_message : config.prompt,
        undefined,{abortSignal:abort.signal,traceContext});
    }
    catch(error) {if(!ownerControl) throw error;}
    if (sessionStore && !ownerControl && config.resume_action!=="continue") {
      const continued = await runtime.continueActiveTargetLoop({trigger:"user-prompt",
        verifyBeforeFirstContinue:true,abortSignal:abort.signal,traceContext});
      if (continued) result = continued;
    }
    return {response:result?.response,turn_id:result?.turnId,upstream_last_turn_usage:result?.usage,session_id:sessionId,
      ...(resumeEvidence ? {resume_evidence:resumeEvidence} : {}),
      ...(ownerControl ? {owner_control:ownerControl} : {}),
      registered_tools:registry.list(),event_count:(await eventStore.getEvents(sessionId)).length,
      persistent_session:!!sessionStore,upstream_target_verification:!!sessionStore,
      owner_wait_seconds:executionTimer.excludedMs/1000,
      target_status:sessionStore ? (await sessionStore.readTarget({sessionID:sessionId}))?.status : null,
      extra_host_tools:false,real_model:config.real_model === true};
  } finally {
    executionTimer.close();
    runtime.beginShutdown();
    sessionStore?.close();
  }
}

export async function selfTest(core,contracts) {
  const spec={type:"function",function:{name:"ryn_fixture_read",description:"Read the one synthetic fixture.",parameters:{type:"object",properties:{},additionalProperties:false}}};
  const calls=[];
  const rpc=async(method,payload)=>{
    calls.push({method,payload});
    if(method==="tool") return {value:"fixture-read-confirmed"};
    if(method==="approval") throw new Error("Readonly fixture must not request approval");
    if(calls.filter(c=>c.method==="model").length===1) return {message:{role:"assistant",content:"",tool_calls:[{id:"fixture-call",type:"function",function:{name:spec.function.name,arguments:"{}"}}]},usage:{input_tokens:12,output_tokens:8}};
    assert(payload.messages.some(m=>m.role==="tool"&&m.content.includes("fixture-read-confirmed")));
    return {message:{role:"assistant",content:"Read fixture confirmed."},usage:{input_tokens:20,output_tokens:6}};
  };
  const result=await runReference(core,contracts,rpc,{session_id:"ryn-reference-protocol-smoke",tools:[spec],tool_metadata:{ryn_fixture_read:{effect:"read"}},provider_id:"ryn-test",model_id:"fixture",context_window:1000000,max_model_calls:16,max_seconds:30,prompt:"Read the synthetic fixture and report its value.",real_model:false});
  assert.equal(calls.filter(c=>c.method==="tool").length,1);
  assert.equal(calls.filter(c=>c.method==="model").length,2);
  assert.deepEqual(fromRynResponse({message:{role:"assistant",content:"ok"}},new Set()).usage,{});
  assert.throws(()=>toRynRequest({messages:[{role:"user",content:[{type:"image",dataUrl:"x"}]}]},new Set()));
  assert.throws(()=>fromRynResponse({message:{role:"assistant",tool_calls:[{function:{name:"Bash",arguments:"{}"}}]}},new Set()));
  const writeSpec={type:"function",function:{name:"ryn_fixture_write",description:"Change the one synthetic fixture, requires approval.",parameters:{type:"object",properties:{enabled:{type:"boolean"}},required:["enabled"],additionalProperties:false}}};
  const approvalTrials=[];
  for(const approved of [true,false]) {
    const trace=[];
    let approvedCall;
    const policy=async(method,payload)=>{
      trace.push({method,payload});
      if(method==="approval") {
        assert.equal(payload.name,"ryn_fixture_write");
        assert.deepEqual(payload.arguments,{enabled:true});
        if(approved) approvedCall=payload.tool_call_id;
        return {approved};
      }
      if(method==="tool") {
        assert.equal(approved,true);
        assert.equal(payload.tool_call_id,approvedCall);
        assert.deepEqual(payload.arguments,{enabled:true});
        return {ok:true};
      }
      if(trace.filter(c=>c.method==="model").length===1) return {message:{role:"assistant",content:"",tool_calls:[{id:"approved-call",type:"function",function:{name:"ryn_fixture_write",arguments:'{"enabled":true}'}}]}};
      assert(payload.messages.some(m=>m.role==="tool"));
      return {message:{role:"assistant",content:approved ? "Write returned." : "Write denied."}};
    };
    await runReference(core,contracts,policy,{session_id:`ryn-reference-policy-${approved}`,tools:[writeSpec],tool_metadata:{ryn_fixture_write:{effect:"write"}},provider_id:"ryn-test",model_id:"fixture",context_window:1000000,max_model_calls:16,max_seconds:30,prompt:"Set the synthetic fixture to enabled.",real_model:false});
    assert.equal(trace.filter(c=>c.method==="approval").length,1);
    assert.equal(trace.filter(c=>c.method==="tool").length,approved ? 1 : 0);
    approvalTrials.push({approved,dispatches:approved ? 1 : 0});
  }
  return {kind:"deterministic-reference-loop-smoke",result,read_model_calls:2,read_tool_calls:1,approvalTrials,real_model:false};
}
