import { createContext } from "react";

export const AgentNavigationContext = createContext({ collapsed: false, toggle: () => {} });
