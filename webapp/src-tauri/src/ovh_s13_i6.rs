use super::*;

#[test]
fn s13_i6_preference_remains_opt_in_and_agent_tasks_keep_awake() {
    assert!(!crate::preferences::Preferences::default().keep_awake);
    let status = RuntimeStatus { node_online: true, active_tasks: 1, ..Default::default() };
    assert!(should_keep_awake(true, &status));
    assert!(!should_keep_awake(false, &status));
}

#[test]
fn s13_i6_long_task_notice_is_delayed_once_and_resets_after_short_work() {
    let now = Instant::now();
    let mut started = None;
    let mut status = RuntimeStatus { node_online: true, active_tasks: 1, ..Default::default() };
    assert!(!long_task_awake_notice(&mut started, &status, false, false, now));
    assert!(!long_task_awake_notice(&mut started, &status, false, false, now + Duration::from_secs(59)));
    assert!(long_task_awake_notice(&mut started, &status, false, false, now + Duration::from_secs(60)));
    assert!(!long_task_awake_notice(&mut started, &status, false, true, now + Duration::from_secs(90)));
    assert!(!long_task_awake_notice(&mut started, &status, true, false, now + Duration::from_secs(120)));
    status.active_tasks = 0;
    assert!(!long_task_awake_notice(&mut started, &status, false, false, now + Duration::from_secs(180)));
    status.active_tasks = 1;
    assert!(!long_task_awake_notice(&mut started, &status, false, false, now + Duration::from_secs(200)));
    let old: crate::preferences::Preferences = serde_json::from_str("{}").unwrap();
    assert!(!old.awake_notice_seen);
    let mut seen = old;
    seen.awake_notice_seen = true;
    let saved = serde_json::to_string(&seen).unwrap();
    let loaded: crate::preferences::Preferences = serde_json::from_str(&saved).unwrap();
    assert!(loaded.awake_notice_seen);
    assert!(!loaded.keep_awake);
}

#[cfg(target_os = "macos")]
#[test]
fn s13_i6_t2_awake_reuses_caffeinate_and_releases_child() {
    let mut controller = new_awake_controller();
    assert!(awake(&mut controller, true));
    let pid = controller.guard.as_ref().unwrap().id();
    assert!(awake(&mut controller, true));
    assert_eq!(controller.guard.as_ref().unwrap().id(), pid);
    assert!(controller.guard.as_mut().unwrap().try_wait().unwrap().is_none());
    assert!(!awake(&mut controller, false));
    assert!(controller.guard.is_none());
    // release() waits for the child, so there is no live process or zombie.
    assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
}
