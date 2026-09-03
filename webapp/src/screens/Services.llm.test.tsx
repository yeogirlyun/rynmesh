import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { LLMOrderResult, LLMProviderStatus, LLMServiceRecord, LLMSetupJob } from "../domain/nodeClient";
import Services from "./Services";

function renderServices(options: {
  configuredNetwork?: string;
  discoveryFailure?: boolean;
  services?: LLMServiceRecord[];
  providerStatus?: LLMProviderStatus;
  orders?: LLMOrderResult[];
  setupStatuses?: LLMSetupJob[];
} = {}) {
  const client = makeFixtureNodeClient();
  const discover = vi.spyOn(client, "listLLMServices");
  if (options.configuredNetwork) {
    const getSettings = client.getSettings.bind(client);
    client.getSettings = vi.fn(async () => ({
      ...await getSettings(),
      network_id: options.configuredNetwork,
    }));
  }
  if (options.discoveryFailure) {
    discover.mockImplementation(async () => {
      throw new Error("LLM discovery unavailable");
    });
  } else if (options.services) {
    discover.mockResolvedValue(options.services);
  }
  if (options.providerStatus) client.getLLMServiceStatus = vi.fn(async () => options.providerStatus!);
  if (options.orders) client.listLLMOrders = vi.fn(async () => options.orders!);
  if (options.setupStatuses) {
    let index = 0;
    client.getLLMSetupStatus = vi.fn(async () => (
      options.setupStatuses![Math.min(index++, options.setupStatuses!.length - 1)]
    ));
  }
  const submit = vi.spyOn(client, "submitLLMOrder");
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [],
    refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(),
    notify: vi.fn(),
  };
  const result = render(
    <MemoryRouter>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route index element={<Services />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { ...result, client, discover, submit, user: userEvent.setup() };
}

describe("Services local LLM flow", () => {
  it("uses the production network default and submits the selected transport policy", async () => {
    const { user, submit } = renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByLabelText("Discovery network")).toHaveValue("rynmesh-main");
    await user.selectOptions(screen.getByLabelText("Transport policy"), "p2p");
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    expect(await screen.findByText(/Strict P2P connection is in progress/)).toBeInTheDocument();
    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      network_id: "rynmesh-main",
      transport: "p2p",
    })));
    expect(await screen.findByText("ice_udp_direct")).toBeInTheDocument();
  });

  it("uses the node's configured network for initial discovery", async () => {
    const { discover } = renderServices({ configuredNetwork: "rynmesh-llm-e2e" });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByLabelText("Discovery network")).toHaveValue("rynmesh-llm-e2e");
    expect(discover).toHaveBeenCalledWith("rynmesh-llm-e2e");
  });

  it("disambiguates equal aliases and submits the exact node and package", async () => {
    const commonService = {
      model_alias: "p2p-direct-host-private-model",
      capabilities: ["chat"],
      context_window: 4096,
      max_output_tokens: 128,
      pricing: {
        currency: "DEV_TASK_BALANCE",
        input_per_1k: 0,
        output_per_1k: 0,
        minimum: 0.001,
        maximum_per_task: 0.01,
      },
      privacy: { policy_text: "Provider sees plaintext during inference." },
    };
    const services: LLMServiceRecord[] = [
      {
        peer_id: "peer-docker",
        node_name: "docker-provider",
        online: true,
        service: { ...commonService, package_id: "e2e-host-real-service" },
      },
      {
        peer_id: "peer-host",
        node_name: "host-native-provider",
        online: true,
        service: { ...commonService, package_id: "e2e-p2p-host-real-service" },
      },
    ];
    const { user, submit } = renderServices({ services });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    const provider = screen.getByLabelText("Provider service");
    expect(screen.getByRole("option", { name: /docker-provider.*e2e-host-real-service/ })).toBeInTheDocument();
    await user.selectOptions(
      provider,
      screen.getByRole("option", { name: /host-native-provider.*e2e-p2p-host-real-service/ }),
    );
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      provider_peer_id: "peer-host",
      service_id: "e2e-p2p-host-real-service",
    })));
  });

  it("explains that an older different-egress package must be updated", async () => {
    const { client, user } = renderServices();
    client.submitLLMOrder = vi.fn(async () => ({
      task_id: "task_shared_exit",
      state: "failed",
      error_code: "p2p_distinct_public_egress_required",
    }));

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    expect(await screen.findByText(/incorrectly requires different public exits/)).toBeInTheDocument();
  });

  it("keeps the Services screen usable when LLM discovery fails", async () => {
    renderServices({ discoveryFailure: true });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByText(/Service discovery failed: LLM discovery unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("configures a local API without publishing it automatically", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "startLLMSetup");
    const publish = vi.spyOn(client, "publishLLMService");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Package ID"));
    await user.type(screen.getByLabelText("Package ID"), "my-local-api");
    await user.clear(screen.getByLabelText("Local API URL"));
    await user.type(screen.getByLabelText("Local API URL"), "http://127.0.0.1:9999");
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      mode: "openai-compatible",
      package_id: "my-local-api",
      base_url: "http://127.0.0.1:9999",
      accept_risk: false,
    })));
    expect(publish).not.toHaveBeenCalled();
  });

  it("polls an asynchronous order and sends cancellation", async () => {
    const { client, user } = renderServices();
    let cancelled = false;
    client.submitLLMOrder = vi.fn(async () => ({ task_id: "task_async", state: "queued" }));
    client.getLLMOrder = vi.fn(async () => ({
      task_id: "task_async", state: cancelled ? "cancelled" : "running",
    }));
    client.cancelLLMOrder = vi.fn(async () => {
      cancelled = true;
      return { task_id: "task_async", state: "cancelled" };
    });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));
    await user.click(await screen.findByRole("button", { name: "Cancel task" }));

    expect(client.cancelLLMOrder).toHaveBeenCalledWith("task_async");
    expect(await screen.findByText(/cancellation requested/)).toBeInTheDocument();
    await waitFor(() => expect(client.getLLMOrder).toHaveBeenCalledWith("task_async"), { timeout: 2000 });
  });

  it("submits authenticated trusted-network local API settings without secret values", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "startLLMSetup");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("API key environment variable (optional)"), "LOCAL_MODEL_KEY");
    await user.click(screen.getByRole("checkbox", { name: /trusted non-loopback API address/i }));
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      api_key_env: "LOCAL_MODEL_KEY",
      allow_non_loopback: true,
    })));
  });

  it("resumes polling a running task discovered after page load", async () => {
    const running = { task_id: "task_resume_after_reload", state: "running" };
    const { client } = renderServices({ orders: [running] });
    client.getLLMOrder = vi.fn(async () => ({
      task_id: running.task_id,
      state: "succeeded",
      output: "resumed result",
      transport: "ice_udp_direct" as const,
    }));

    expect(await screen.findByText("resumed result")).toBeInTheDocument();
    expect(client.getLLMOrder).toHaveBeenCalledWith(running.task_id);
  });

  it("shows recovered setup progress and Provider lifecycle controls", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0].service;
    const providerStatus: LLMProviderStatus = {
      configured: true,
      online: true,
      publication_enabled: false,
      service,
      lifecycle: { runtime: { managed: true, installed: true, running: true, status: "running" } },
    };
    const { client, user } = renderServices({
      providerStatus,
      setupStatuses: [
        { job_id: "setup_resume", state: "running", stage: "download_model", progress: 55, message: "Downloading verified model data" },
        { job_id: "setup_resume", state: "succeeded", stage: "completed", progress: 100, message: "Local model is ready" },
      ],
    });
    const action = vi.spyOn(client, "runLLMServiceAction");

    expect(await screen.findByText("Local model is ready")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run self-test" }));
    expect(action).toHaveBeenCalledWith("self-test");
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart" })).toBeEnabled());
  });

  it("blocks an order before submission when the Provider is offline", async () => {
    const service = (await makeFixtureNodeClient().listLLMServices())[0];
    const { submit } = renderServices({ services: [{ ...service, online: false }] });

    expect(await screen.findByText("Provider offline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Place encrypted order" })).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("labels the managed setup option as a bundled-runtime local model, not Docker", async () => {
    renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Managed local model (bundled runtime)" })).toBeInTheDocument();
    expect(screen.queryByText(/Optional managed Docker model/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Docker Desktop\/Engine must already be installed and running/)).not.toBeInTheDocument();
  });

  it("shows the bundled runtime helper text and profile select for managed setup", async () => {
    const { user } = renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Setup mode"), "managed");

    expect(await screen.findByText(
      "Downloads a verified model and runs it with the bundled llama.cpp runtime on this device. Docker is only used on server nodes that choose it.",
    )).toBeInTheDocument();
    expect(screen.getByLabelText("Model profile")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Balanced.*recommended/ })).toBeInTheDocument();
  });

  it("shows whether the bundled runtime is already available", async () => {
    renderServices();

    expect(await screen.findByText("Bundled runtime: available")).toBeInTheDocument();
  });

  it("posts the selected profile in the managed setup body", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "startLLMSetup");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Setup mode"), "managed");
    await user.selectOptions(screen.getByLabelText("Model profile"), "balanced");
    await user.click(screen.getByRole("checkbox", { name: /prepares a local runtime/i }));
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      mode: "managed",
      profile: "balanced",
    })));
  });

  it.each([
    {
      raw: "no local inference runtime is available: nothing resolvable",
      mapped: "No local inference runtime is available on this device yet. Retry to download the bundled runtime, or connect an existing local model API.",
    },
    {
      raw: "runtime archive checksum mismatch",
      mapped: "A download failed verification and was discarded. Retry to download it again.",
    },
    {
      raw: "model checksum mismatch; the download was quarantined and will restart",
      mapped: "A download failed verification and was discarded. Retry to download it again.",
    },
    {
      raw: "llama-server exited during startup (see the runtime log)",
      mapped: "The local model runtime stopped while starting. Try a smaller model profile or check the runtime log from Settings.",
    },
    {
      raw: "download exceeded the pinned size",
      mapped: "The download did not match the expected size and was discarded. Retry.",
    },
  ])("maps the native runtime error '$raw'", async ({ raw, mapped }) => {
    renderServices({
      setupStatuses: [
        { job_id: "setup_native_error", state: "failed", stage: "download_model", progress: 40, message: raw, retryable: true },
      ],
    });

    expect(await screen.findByText(mapped)).toBeInTheDocument();
  });
});
