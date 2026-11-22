// @ts-check
import { defineEcConfig } from '@astrojs/starlight/expressive-code';
import {
  pluginCollapsibleSections,
  pluginCollapsibleSectionsTexts,
} from '@expressive-code/plugin-collapsible-sections';
import { pluginLineNumbers } from '@expressive-code/plugin-line-numbers';

pluginCollapsibleSectionsTexts.addLocale('zh-CN', {
  collapsedLines: '已折叠 {lineCount} 行代码',
});

export default defineEcConfig({
  themes: ['night-owl', 'night-owl-light'],
  useStarlightUiThemeColors: true,
  plugins: [pluginCollapsibleSections(), pluginLineNumbers()],
  defaultProps: {
    collapseStyle: 'collapsible-auto',
    showLineNumbers: false,
  },
});
