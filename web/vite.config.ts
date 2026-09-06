import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  base: './',
  plugins: [
    vue(),
    {
      name: 'mini-im-webengine-html',
      enforce: 'post',
      transformIndexHtml(html, ctx) {
        if (!ctx.bundle) {
          return html;
        }
        let result = html
          .replace(/<script type="module"/g, '<script')
          .replace(/\s+crossorigin/g, '');
        for (const item of Object.values(ctx.bundle)) {
          if (item.type === 'chunk' && item.isEntry) {
            const code = item.code.replace(/<\/script/gi, '<\\\\/script');
            result = result.replace(/<script[^>]+src="[^"]+"[^>]*><\/script>/, '');
            result = result.replace('</body>', () => `    <script>${code}</script>\n  </body>`);
          }
          if (item.type === 'asset' && item.fileName.endsWith('.css') && typeof item.source === 'string') {
            result = result.replace(
              /<link[^>]+href="[^"]+"[^>]*>/,
              () => `<style>${item.source}</style>`
            );
          }
        }
        return result;
      }
    }
  ],
  build: {
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        format: 'iife',
        inlineDynamicImports: true
      }
    }
  },
  server: {
    host: '127.0.0.1',
    port: 5173
  }
});
