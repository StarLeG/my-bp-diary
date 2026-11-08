// Конфиг сборки Tailwind CSS лендинга (SPEC.md, §16.1, «SEO и скорость»).
// Собирается tools/landing/build_css.py → web/landing/css/tailwind.css.
const path = require("path");

const landing = path.join(__dirname, "..", "..", "web", "landing");

module.exports = {
  content: [path.join(landing, "*.html"), path.join(landing, "script.js")],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "system-ui", "-apple-system", "sans-serif"] },
    },
  },
};
