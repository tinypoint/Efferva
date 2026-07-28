import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

const site = process.env.SITE_URL ?? "http://localhost:4321";
const base = normalizeBase(process.env.BASE_PATH ?? "/");
const asset = (path) => `${base === "/" ? "" : base}${path}`;
const socialImage = new URL(asset("/og.png"), site).toString();

export default defineConfig({
  site,
  base,
  output: "static",
  integrations: [
    starlight({
      title: "Efferva",
      description:
        "The embeddable agent runtime for durable, multi-tenant products.",
      favicon: asset("/favicon-32.png"),
      logo: {
        src: "./src/assets/efferva-mark.png",
        alt: "Efferva",
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/tinypoint/Efferva",
        },
      ],
      editLink: {
        baseUrl:
          "https://github.com/tinypoint/Efferva/edit/main/website/",
      },
      locales: {
        root: {
          label: "English",
          lang: "en",
        },
        "zh-cn": {
          label: "简体中文",
          lang: "zh-CN",
        },
      },
      sidebar: [
        {
          label: "Documentation",
          translations: {
            "zh-cn": "文档",
          },
          items: [
            {
              autogenerate: {
                directory: "docs",
              },
            },
          ],
        },
      ],
      customCss: ["./src/styles/starlight.css"],
      head: [
        {
          tag: "meta",
          attrs: {
            property: "og:image",
            content: socialImage,
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:card",
            content: "summary_large_image",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:image",
            content: socialImage,
          },
        },
      ],
    }),
  ],
});

function normalizeBase(value) {
  const normalized = `/${value}`.replace(/\/+/g, "/");
  if (normalized === "/") {
    return "/";
  }
  return normalized.replace(/\/$/, "");
}
