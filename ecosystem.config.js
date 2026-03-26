/**
 * PM2 プロセス管理設定
 *
 * 起動: pm2 start ecosystem.config.js
 * 停止: pm2 stop alpha-strike
 * 状態確認: pm2 list
 * ログ確認: pm2 logs alpha-strike
 * OS 再起動後に自動起動: pm2 startup && pm2 save
 */
module.exports = {
  apps: [
    {
      name: "alpha-strike",
      script: "uv",
      args: "run uvicorn webhook_server:app --host 0.0.0.0 --port 8080",
      interpreter: "none",
      cwd: __dirname,

      // プロセス監視・自動再起動
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000, // 5秒待ってから再起動
      min_uptime: "10s", // 10秒未満で落ちた場合はクラッシュループとみなす

      // ログ設定
      log_file: "logs/alpha-strike.log",
      error_file: "logs/alpha-strike-error.log",
      time: true, // タイムスタンプ付きログ
      log_date_format: "YYYY-MM-DD HH:mm:ss",

      // 環境変数は .env ファイルまたはシステム環境変数で管理する
      env: {},
    },
  ],
};
