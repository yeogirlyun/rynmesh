// These are application-owned values, kept stable for filtering and status styles.
// Device names, notes, model identifiers and provider content are never translated here.
export const personalLabelKeys: Record<string, string> = {
  All: "all", Overview: "overview", "API access": "apiAccess",
  "My devices": "myDevices", "Shared with me": "sharedWithMe", Discovered: "discovered",
  "Requested by me": "requestedByMe", "Running on this device": "runningOnThisDevice",
  "In progress": "inProgress", Completed: "completed", Failed: "failed",
  Cancelled: "cancelled", Processing: "processing", Ready: "ready", Offline: "offline",
  Available: "available", "Access granted": "accessGranted",
  "Current network": "currentNetwork", "Network unconfirmed": "networkUnconfirmed",
  "Public endpoints": "publicEndpoints", "Home network": "homeNetwork", "Office network": "officeNetwork",
  "This device": "thisDevice", "LAN direct": "lanDirect", "Public direct": "publicDirect",
  Relay: "relay", "Route unconfirmed": "routeUnconfirmed",
  "Enter a device name.": "enterADeviceName",
  "Use 32 characters or fewer for the name.": "use32CharactersOrFewerForTheName",
  "Use 200 characters or fewer for the private note.": "use200CharactersOrFewerForThePrivateNote",
  "Some device or service information could not be refreshed.": "someDeviceOrServiceInformationCouldNotBeRefreshed",
};
