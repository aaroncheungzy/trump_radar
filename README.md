# Donald J. Trump言论监控与金融分析系统

A powerful tool that automatically monitors news sources, translates articles, analyzes economic impacts, and integrates real-time financial data—all with one-click execution and GitHub Actions support.

## 核心功能

✅ **自动新闻抓取**：监控指定RSS源，智能识别未处理的新文章  
✅ **中英文翻译**：基于豆包/DeepSeek双模型的精准翻译  
✅ **经济影响分析**：结合新闻内容与金融数据生成专业分析报告  
✅ **实时金融数据**：获取指数（纳斯达克）与加密货币（BTC/ETH/SOL/XRP）实时价格及24h涨跌幅  
✅ **自动邮件推送**：生成HTML格式报告并发送至指定邮箱  
✅ **错误重试机制**：网络故障时自动重试，确保任务完成  
✅ **GitHub Actions适配**：支持云端定时运行，无需本地部署  

## 项目结构

```
news-monitor-system/
├── .github/
│   └── workflows/
│       └── run.yml          # GitHub Actions工作流配置
├── config.json             # 核心配置文件（所有参数集中管理）
├── news_monitor.py         # 主程序文件
├── requirements.txt        # 依赖库列表
└── README.md               # 项目说明文档
```

## 快速开始

### 1. 环境准备

- Python 3.8+
- 依赖安装：
  ```bash
  pip install -r requirements.txt
  ```

### 2. 配置文件设置

复制并修改 `config.json`，填写必要参数：

```json
{
  "target_url": "https://trumpstruth.org/feed",  // 监控的RSS源URL
  "history_file": "processed_articles.json",     // 已处理文章记录文件
  
  "thresholds": {
    "content_length": 50,                       // 文章最小长度阈值
    "max_recent_ids": 1000,                     // 最大缓存ID数量
    "deepseek_timeout": 60,                     // DeepSeek API超时时间
    "doubao_timeout": 60,                       // 豆包API超时时间
    "api_retry_times": 3,                       // API重试次数
    "financial_data_timeout": 30                // 金融数据超时时间
  },
  
  "financial_assets": {                          // 可自定义监控的金融资产
    "^IXIC": {"name": "纳斯达克指数", "type": "指数"},
    "BTC-USD": {"name": "比特币", "type": "加密货币"},
    "ETH-USD": {"name": "以太币", "type": "加密货币"},
    "SOL-USD": {"name": "SOL", "type": "加密货币"},
    "XRP-USD": {"name": "XRP", "type": "加密货币"}
  },
  
  "email": {
    "smtp_server": "",                          // SMTP服务器（留空自动识别）
    "smtp_port": "",                            // SMTP端口（留空自动识别）
    "from": "your-email@example.com",            // 发件人邮箱
    "password": "your-password/app-key",         // 邮箱密码/授权码
    "to": "recipient1@example.com,recipient2@example.com"  // 收件人（多个用逗号分隔）
  },
  
  "ai_models": {
    "doubao_api_key": "your-doubao-api-key",     // 豆包API密钥
    "deepseek_api_key": "your-deepseek-api-key"  // DeepSeek API密钥
  }
}
```

### 3. 本地运行

直接执行主程序，自动完成全流程：
```bash
python news_monitor.py
```

## GitHub Actions 部署（推荐）

### 1. 仓库准备

1. Fork本仓库到你的GitHub账号
2. 在仓库中添加以下 **Secrets**（`Settings → Secrets and variables → Actions`）：

| Secret名称          | 说明                                  |
|---------------------|---------------------------------------|
| `EMAIL_FROM`        | 发件人邮箱（与config.json一致）        |
| `EMAIL_PASSWORD`    | 邮箱密码/授权码                       |
| `EMAIL_TO`          | 收件人邮箱（多个用逗号分隔）           |
| `DOUBAO_API_KEY`    | 豆包API密钥                           |
| `DEEPSEEK_API_KEY`  | DeepSeek API密钥                       |
| `HTTP_PROXY`（可选）| 代理地址（如需要访问境外资源）         |
| `HTTPS_PROXY`（可选）| 代理地址                              |

### 2. 自动运行配置

工作流默认配置：
- 每日北京时间10:00和22:00自动运行
- 支持手动触发（`Actions → 新闻监控与分析任务 → Run workflow`）

如需修改运行时间，编辑 `.github/workflows/run.yml` 中的 `schedule` 字段：
```yaml
schedule:
  - cron: '0 2 * * *'   # 北京时间10:00（UTC+8 → UTC 02:00）
  - cron: '0 14 * * *'  # 北京时间22:00（UTC+8 → UTC 14:00）
```

### 3. 查看运行结果

- 运行日志：`Actions → 选择对应运行记录 → 查看详细日志`
- 输出文件：每次运行后会自动上传 `news_monitor.log`（日志）、`processed_articles.json`（历史记录）等文件
- 邮件通知：运行完成后会自动发送HTML格式报告到指定邮箱

## 关键特性说明

### 1. 智能新闻处理
- 基于文章标题+发布时间生成唯一ID，避免重复处理
- 自动过滤短内容文章（可通过`content_length`调整阈值）
- 支持RSS源增量更新检测

### 2. 双AI模型保障
- 优先使用豆包模型，失败自动切换到DeepSeek
- 内置模型连通性测试，确保服务可用性
- 支持超时重试与错误捕获

### 3. 金融数据集成
- 实时获取资产当前价格、24h涨跌额、24h涨跌幅
- 自动格式化数据展示，涨跌颜色标记（红涨绿跌）
- 支持自定义添加/删除金融资产（需使用yfinance支持的代码）

### 4. 灵活配置
- 所有参数集中在`config.json`，无需修改代码
- SMTP服务器自动识别（支持主流邮箱：Gmail/Outlook/iCloud/QQ/163等）
- 支持本地运行与GitHub Actions云端运行无缝切换

## 注意事项

1. **API密钥**：确保豆包/DeepSeek API密钥有效且余额充足
2. **邮箱配置**：
   - 部分邮箱（如Gmail）需要开启"不太安全的应用访问"或使用应用专用密码
   - 企业邮箱可能需要手动配置SMTP服务器和端口
3. **网络访问**：
   - 目标RSS源和金融数据接口可能需要科学上网
   - GitHub Actions运行时可通过配置`HTTP_PROXY`和`HTTPS_PROXY` Secrets解决
4. **历史记录**：`processed_articles.json`文件记录已处理文章，删除该文件会重新处理所有文章

## 常见问题排查

### Q1: 无法获取新闻内容
- 检查`target_url`是否有效（手动访问确认RSS源可访问）
- 检查网络连接或代理配置
- 查看`news_monitor.log`中的具体错误信息

### Q2: 邮件发送失败
- 验证SMTP服务器和端口是否正确
- 确认邮箱密码/授权码是否有效
- 检查收件人邮箱格式是否正确（多个用逗号分隔）

### Q3: 金融数据获取失败
- 检查网络是否能访问Yahoo Finance
- 确认金融资产代码是否正确（参考[yfinance支持的代码](https://finance.yahoo.com/)）
- 查看日志中的具体错误信息

### Q4: GitHub Actions运行失败
- 检查Secrets是否配置正确
- 查看运行日志中的错误信息
- 确认依赖安装是否成功

## 许可证

[MIT](LICENSE)

## 贡献

欢迎提交Issue或Pull Request，一起完善这个项目！

