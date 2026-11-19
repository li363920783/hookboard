import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MantineProvider, createTheme } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./index.css";
import App from "./App";

const theme = createTheme({
  primaryColor: "indigo",
  defaultRadius: "lg",
  radius: { xs: "6px", sm: "8px", md: "12px", lg: "18px", xl: "22px" },
  fontFamily:
    "Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif",
  fontFamilyMonospace:
    "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace",
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider defaultColorScheme="dark" theme={theme}>
      <Notifications position="bottom-center" limit={3} autoClose={4200} />
      <App />
    </MantineProvider>
  </StrictMode>,
);