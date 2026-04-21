module.exports = async (kernel) => {
  let port = 3232;
  const fs = require("fs");
  const path = require("path");
  const envPath = path.join(__dirname, ".env");

  if (fs.existsSync(envPath)) {
    const envContent = fs.readFileSync(envPath, "utf8");
    const portMatch = envContent.match(/^\s*PORT\s*=\s*(\d+)\s*$/m);
    if (portMatch) {
      port = parseInt(portMatch[1], 10);
    }
  }

  return {
    daemon: true,
    run: [
      {
        method: "shell.run",
        params: {
          path: ".",
          venv: "venv",
          message: [
            "python app.py"
          ]
        }
      },
      {
        method: "local.set",
        params: {
          url: `http://localhost:${port}`
        }
      },
      {
        method: "notify",
        params: {
          html: `VRest is running at <a href=\"http://localhost:${port}\" target=\"_blank\">http://localhost:${port}</a>`
        }
      }
    ]
  };
};
