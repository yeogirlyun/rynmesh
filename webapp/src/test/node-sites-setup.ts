import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "../locales/sites.en.json";
i18n.use(initReactI18next).init({ lng: "en", resources: { en: { translation: { sites: en } } }, interpolation: { escapeValue: false } });
afterEach(cleanup);
