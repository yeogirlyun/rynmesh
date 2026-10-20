use ryn_computer_use::engine::{OperationControlTable, PhysicalActorEngine};
use ryn_computer_use::errors::{DiagnosticReason, ErrorCode, ProtocolError};
use ryn_computer_use::protocol::{
    parse_and_validate_request_line, read_bounded_jsonl_frame, BoundedFrameRead, RequestEnvelope,
    RequestId, ResponseEnvelope, MAX_COMMAND_LINE_BYTES, MAX_RESPONSE_FRAME_BYTES,
};
use serde::Serialize;
use std::io::{self, BufWriter, Write};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::Duration;

enum ActorCommand {
    Request(RequestEnvelope),
    Eof,
}

type SharedStdout = Arc<Mutex<BufWriter<io::Stdout>>>;

fn write_wire_frame<T: Serialize>(
    out: &SharedStdout,
    frame: &T,
    fallback_id: Option<RequestId>,
) {
    let serialized = match serde_json::to_string(frame) {
        Ok(s) => s,
        Err(_) => return,
    };

    if serialized.len() > MAX_RESPONSE_FRAME_BYTES {
        if let Some(id) = fallback_id {
            let err = ProtocolError::protocol(
                ErrorCode::InvalidRequest,
                DiagnosticReason::FrameTooLarge,
            );
            let resp = ResponseEnvelope::err(id, err);
            if let Ok(fallback_str) = serde_json::to_string(&resp) {
                if let Ok(mut guard) = out.lock() {
                    let _ = guard.write_all(fallback_str.as_bytes());
                    let _ = guard.write_all(b"\n");
                    let _ = guard.flush();
                }
            }
        }
        return;
    }

    if let Ok(mut guard) = out.lock() {
        let _ = guard.write_all(serialized.as_bytes());
        let _ = guard.write_all(b"\n");
        let _ = guard.flush();
    }
}

fn pre_signal_from_reader(control: &OperationControlTable, env: &RequestEnvelope) {
    match env.method.as_str() {
        "action.prepare" => {
            if let (Some(task_id), Some(op_id)) = (
                env.params.get("taskSessionId").and_then(|v| v.as_str()),
                env.params.get("operationId").and_then(|v| v.as_str()),
            ) {
                let _ = control.register_operation(task_id, op_id);
            }
        }
        "action.cancel" => {
            if let (Some(task_id), Some(op_id)) = (
                env.params.get("taskSessionId").and_then(|v| v.as_str()),
                env.params.get("operationId").and_then(|v| v.as_str()),
            ) {
                control.signal_cancel_operation(task_id, op_id);
            }
        }
        "session.end" => {
            if let Some(task_id) = env.params.get("taskSessionId").and_then(|v| v.as_str()) {
                control.signal_cancel_task(task_id);
            }
        }
        "shutdown" => {
            control.signal_shutdown_all();
        }
        _ => {}
    }
}

fn main() {
    let stdout_writer: SharedStdout = Arc::new(Mutex::new(BufWriter::new(io::stdout())));
    let control = OperationControlTable::new();
    let (tx, rx) = mpsc::channel::<ActorCommand>();

    let actor_out = stdout_writer.clone();
    let actor_control = control.clone();

    // Dedicated physical actor thread: owns PhysicalActorEngine and any !Send DesktopInputLockGuard
    // across prepare -> ready -> commit / cancel / expiry / EOF.
    let actor_handle = std::thread::Builder::new()
        .name("ryn-physical-actor".to_string())
        .spawn(move || {
            let mut engine = match PhysicalActorEngine::new(actor_control) {
                Ok(e) => e,
                Err(init_err) => {
                    while let Ok(cmd) = rx.recv() {
                        match cmd {
                            ActorCommand::Request(req) => {
                                let resp = ResponseEnvelope::err(req.id, init_err.clone());
                                write_wire_frame(&actor_out, &resp, None);
                                if req.method == "shutdown" {
                                    break;
                                }
                            }
                            ActorCommand::Eof => break,
                        }
                    }
                    return;
                }
            };

            loop {
                match rx.recv_timeout(Duration::from_millis(20)) {
                    Ok(ActorCommand::Request(req)) => {
                        engine.poll_maintenance();
                        let req_id_clone = req.id.clone();
                        let resp = engine.handle_request(req, &mut |ev| {
                            write_wire_frame(&actor_out, &ev, None);
                        });
                        write_wire_frame(&actor_out, &resp, Some(req_id_clone));
                        if engine.is_shutting_down() {
                            break;
                        }
                    }
                    Ok(ActorCommand::Eof) => {
                        engine.handle_eof();
                        break;
                    }
                    Err(RecvTimeoutError::Timeout) => {
                        engine.poll_maintenance();
                        if engine.is_shutting_down() {
                            break;
                        }
                    }
                    Err(RecvTimeoutError::Disconnected) => {
                        engine.handle_eof();
                        break;
                    }
                }
            }
        })
        .expect("Failed to spawn physical actor thread");

    // Independent stdin reader loop: enforces MAX_COMMAND_LINE_BYTES (64 KiB) at stream read
    // boundary (`cap + 1`) via `read_bounded_jsonl_frame` so oversized or unterminated inputs
    // never allocate unbounded memory or hang waiting for a newline.
    let stdin = io::stdin();
    let mut reader = io::BufReader::new(stdin.lock());
    let mut scratch = Vec::with_capacity(MAX_COMMAND_LINE_BYTES.saturating_add(2));

    loop {
        match read_bounded_jsonl_frame(&mut reader, MAX_COMMAND_LINE_BYTES, &mut scratch) {
            Ok(BoundedFrameRead::Eof) => {
                control.signal_shutdown_all();
                let _ = tx.send(ActorCommand::Eof);
                break;
            }
            Ok(BoundedFrameRead::Oversized) => {
                // Fail boundedly with one safe protocol error and close session; never resynchronize
                // a trailing fragment of an oversized frame into a new request.
                let err = ProtocolError::protocol(
                    ErrorCode::InvalidRequest,
                    DiagnosticReason::FrameTooLarge,
                );
                let resp = ResponseEnvelope::err(RequestId::Int(0), err);
                write_wire_frame(&stdout_writer, &resp, None);
                control.signal_shutdown_all();
                let _ = tx.send(ActorCommand::Eof);
                break;
            }
            Ok(BoundedFrameRead::UnterminatedEof) | Ok(BoundedFrameRead::InvalidUtf8) => {
                let err = ProtocolError::protocol(
                    ErrorCode::InvalidRequest,
                    DiagnosticReason::MalformedJson,
                );
                let resp = ResponseEnvelope::err(RequestId::Int(0), err);
                write_wire_frame(&stdout_writer, &resp, None);
                control.signal_shutdown_all();
                let _ = tx.send(ActorCommand::Eof);
                break;
            }
            Ok(BoundedFrameRead::Line(line)) => {
                if line.trim().is_empty() {
                    continue;
                }
                match parse_and_validate_request_line(&line) {
                    Ok(env) => {
                        let is_shutdown = env.method == "shutdown";
                        pre_signal_from_reader(&control, &env);
                        if tx.send(ActorCommand::Request(env)).is_err() {
                            break;
                        }
                        if is_shutdown {
                            break;
                        }
                    }
                    Err((opt_id, err)) => {
                        let id = opt_id.unwrap_or(RequestId::Int(0));
                        let resp = ResponseEnvelope::err(id, err);
                        write_wire_frame(&stdout_writer, &resp, None);
                    }
                }
            }
            Err(_) => {
                control.signal_shutdown_all();
                let _ = tx.send(ActorCommand::Eof);
                break;
            }
        }
    }

    let _ = actor_handle.join();
}
