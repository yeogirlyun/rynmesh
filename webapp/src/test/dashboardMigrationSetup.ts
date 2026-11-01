import '@testing-library/jest-dom/vitest';
import {afterEach,beforeEach} from 'vitest';
import {cleanup} from '@testing-library/react';
import i18n from '../i18n';
beforeEach(()=>{void i18n.changeLanguage('en');});
afterEach(cleanup);
