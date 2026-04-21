module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        path: ".",
        message: [
          "if exist venv rmdir /s /q venv",
          "if exist data rmdir /s /q data",
          "mkdir data"
        ]
      }
    }
  ]
};
