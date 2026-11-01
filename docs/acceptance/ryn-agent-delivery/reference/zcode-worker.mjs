import {createInterface} from "node:readline";
import {openSync, writeSync, fsyncSync, closeSync} from "node:fs";
import {runReference} from "./zcode-adapter.mjs";

// JSON lines over inherited pipes; there is no listening socket and no secret input.
// Package exports are passed by a launcher whose cwd is the pinned core package.
export async function worker(core,contracts,storage) {
  const lines=createInterface({input:process.stdin,crlfDelay:Infinity});
  const pending=new Map();
  let sequence=0,resolveConfig,config,auditFile,controlHandler;
  const configPromise=new Promise(resolve=>{resolveConfig=resolve;});
  lines.on("line",line=>{
    try {
      if(Buffer.byteLength(line)>2*1024*1024) throw new Error("wire_size_limit");
      const packet=JSON.parse(line);
      if(packet.kind==="config"&&resolveConfig) {resolveConfig(packet.value);resolveConfig=null;return;}
      if(packet.kind==="control") {
        if(!controlHandler || !["pause","cancel","steer"].includes(packet.action)) throw new Error("wire_invalid_control");
        controlHandler(packet.action);
        // Stop also closes unanswered host waits (notably manual approval).
        // Reject the outstanding promise; never synthesize an approval/result.
        for(const receiver of pending.values()) receiver.reject(new Error("owner_cancelled"));
        pending.clear();return;
      }
      const receiver=pending.get(packet.id);
      if(!receiver) throw new Error("wire_unknown_response");
      if(packet.event) {
        if(!receiver.onEvent || packet.event.type!=="text_delta" || typeof packet.event.text!=="string") throw new Error("wire_invalid_event");
        receiver.onEvent(packet.event);return;
      }
      pending.delete(packet.id);
      if(packet.error) receiver.reject(new Error("ryn_gateway_rejected"));
      else receiver.resolve(packet.result);
    } catch {
      for(const receiver of pending.values()) receiver.reject(new Error("wire_invalid_packet"));
      pending.clear();
      process.exitCode=1;
    }
  });
  const rpc=(method,params,onEvent)=>new Promise((resolve,reject)=>{
    const id=++sequence;
    let observation;
    if (config.dispatch_provenance && method!=="checkpoint") {
      const source=config.dispatch_provenance;
      observation={id:`${config.session_id}:rpc:${id}`,rpc_id:id,session_id:config.session_id,method,
        sampling_binding:source.sampling_binding,task_id:source.task_id,
        registration_sha256:source.registration_sha256,emitted_at:new Date().toISOString()};
      // Contains no messages, arguments, responses, reasoning or credentials.
      writeSync(auditFile,JSON.stringify(observation)+"\n");
      fsyncSync(auditFile);
    }
    pending.set(id,{resolve,reject,onEvent});
    process.stdout.write(JSON.stringify({kind:"rpc",id,method,params,
      ...(observation ? {dispatch_provenance:observation} : {})})+"\n");
  });
  rpc.setControlHandler=handler=>{controlHandler=handler;};
  lines.on("close",()=>{
    for(const receiver of pending.values()) receiver.reject(new Error("wire_closed"));
    pending.clear();
  });
  try {
    config=await configPromise;
    sequence=config.rpc_sequence_start ?? 0;
    if(!Number.isSafeInteger(sequence) || sequence<0) throw new Error("wire_invalid_sequence");
    // Resume here is readback-only: it emits no dispatch and must not recreate
    // the original exclusive dispatch audit file or claim a new model request.
    if(config.dispatch_provenance && (!config.resume || config.resume_action)) auditFile=openSync(config.dispatch_provenance.audit_path,"wx");
    const result=await runReference(core,contracts,rpc,config,storage);
    process.stdout.write(JSON.stringify({kind:"result",result})+"\n");
  } catch {
    // Gateway errors and provider payloads can contain private material. The
    // trusted parent already records safe codes and raw synthetic evidence.
    process.stdout.write(JSON.stringify({kind:"error",error:"reference_execution_failed"})+"\n");
    process.exitCode=1;
  } finally {if(auditFile!==undefined) closeSync(auditFile);lines.close();}
}
