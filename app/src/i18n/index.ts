import i18n from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { initReactI18next } from 'react-i18next';
import de from './locales/de/translation.json';
import en from './locales/en/translation.json';
import ja from './locales/ja/translation.json';
import zhCN from './locales/zh-CN/translation.json';
import zhTW from './locales/zh-TW/translation.json';

export const SUPPORTED_LANGUAGES = [
  { code: 'de', label: 'Deutsch' },
  { code: 'en', label: 'English' },
  { code: 'ja', label: '日本語' },
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
] as const;

export type LanguageCode = (typeof SUPPORTED_LANGUAGES)[number]['code'];

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      // USCRX-2026-16039: Produkt = deutsche Beschriftung. 'de-DE' ist ein
      // eigener Schlüssel, weil der LanguageDetector BCP-47 ausliefert und
      // `load: 'currentOnly'` (wegen zh-CN/zh-TW) keine Regionen abschneidet.
      de: { translation: de },
      'de-DE': { translation: de },
      en: { translation: en },
      ja: { translation: ja },
      'zh-CN': { translation: zhCN },
      'zh-TW': { translation: zhTW },
    },
    fallbackLng: 'de',
    supportedLngs: [...SUPPORTED_LANGUAGES.map((l) => l.code), 'de-DE'],
    load: 'currentOnly',
    interpolation: { escapeValue: false },
    react: { useSuspense: false },
    detection: {
      order: ['localStorage', 'navigator'],
      lookupLocalStorage: 'voicebox:lang',
      caches: ['localStorage'],
    },
  });

export default i18n;
