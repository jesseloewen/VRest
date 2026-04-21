module.exports = {
  version: "1.0",
  title: "VRest - Video Browser",
  description: "Flask video browser with previews, streaming, and subtitle support.",
  icon: "",
  menu: async (kernel, info) => {
    const installed = info.exists("venv") && info.exists("requirements.txt");
    const running = {
      start: info.running("start.js"),
      reset: info.running("reset.js")
    };

    if (installed) {
      if (running.start) {
        return [
          {
            default: true,
            icon: "fa-solid fa-terminal",
            text: "Server Running",
            href: "start.js"
          },
          {
            icon: "fa-solid fa-globe",
            text: "Open Web UI",
            href: "{{local.url}}"
          }
        ];
      }

      return [
        {
          default: true,
          icon: "fa-solid fa-power-off",
          text: "Start Server",
          href: "start.js"
        },
        {
          icon: "fa-regular fa-circle-xmark",
          text: "Reset",
          href: "reset.js",
          confirm: "Are you sure you want to reset? This will remove venv and generated cache data."
        }
      ];
    }

    if (running.reset) {
      return [
        {
          default: true,
          icon: "fa-solid fa-rotate",
          text: "Resetting...",
          href: "reset.js"
        }
      ];
    }

    return [
      {
        default: true,
        icon: "fa-solid fa-download",
        text: "Install",
        href: "install.js"
      }
    ];
  }
};
