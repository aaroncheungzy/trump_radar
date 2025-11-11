# 新闻监控与金融分析系统

一个自动化监控指定新闻源、翻译分析新文章，并结合实时金融数据提供市场洞察的工具。

## 功能特点

- 定时监控指定新闻源，自动抓取新文章
- 自动翻译英文文章为中文
- 结合新闻内容进行经济影响分析
- 实时获取指定金融资产数据（指数、加密货币等）
- 生成包含新闻翻译、市场分析和金融数据的汇总报告
- 自动发送邮件报告
- 网络故障时自动重试机制

## 支持的金融资产

默认监控以下资产，可在配置中自定义：
- 纳斯达克指数
- 比特币 (BTC)
- 以太币 (ETH)
- SOL
- XRP

## 环境要求

- Python 3.8+
- 所需依赖库：`requirements.txt`中列出

## 安装步骤

1. 克隆或下载项目代码
```bash
git clone <仓库地址>
cd news-monitor-system
```

2. 安装依赖包
```bash
pip install -r requirements.txt
```

3. 配置参数（见下方配置说明）

## 配置说明

在主程序文件的`if __name__ == "__main__"`部分配置以下参数：

### 1. 代理配置（可选）
```python
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
```

### 2. 阈值参数
```python
thresholds = {
    "content_length": 50,  # 文章内容最小长度阈值
    "max_recent_ids": 1000,  # 最大近期文章ID缓存数
    "deepseek_timeout": 60,  # DeepSeek API超时时间(秒)
    "doubao_timeout": 60,  # 豆包API超时时间(秒)
    "api_retry_times": 3,  # API调用重试次数
    "financial_data_timeout": 30  # 金融数据获取超时时间(秒)
}
```

### 3. 金融资产配置
```python
financial_assets = {
    "^IXIC": {"name": "纳斯达克指数", "type": "指数"},
    "BTC-USD": {"name": "比特币", "type": "加密货币"},
    "ETH-USD": {"name": "以太币", "type": "加密货币"},
    "SOL-USD": {"name": "SOL", "type": "加密货币"},
    "XRP-USD": {"name": "XRP", "type": "加密货币"}
}
```

### 4. 核心配置
```python
config = {
    "target_url": "https://trumpstruth.org/feed",  # 监控的新闻源URL
    "summary_times": ["10:00", "22:00"],  # 每日检查时间（北京时间）
    "history_file": "processed_articles.json",  # 已处理文章记录文件
    "deepseek_api_key": "your_deepseek_api_key",  # DeepSeek API密钥
    "doubao_api_key": "your_doubao_api_key",  # 豆包API密钥
    
    # 邮件配置
    "email_smtp_server": "smtp.mail.me.com",
    "email_smtp_port": 587,
    "sender_email": "your_email@example.com",
    "sender_password": "your_email_password",
    "email_receivers": ["recipient1@example.com", "recipient2@example.com"]
}
```

## 使用方法

直接运行主程序：
```bash
python news_monitor.py
```

程序将按照配置的时间点（`summary_times`）自动执行以下操作：
1. 抓取新闻源内容（失败将每5分钟重试）
2. 识别并提取新文章
3. 翻译新文章为中文
4. 获取实时金融资产数据
5. 结合新闻和金融数据进行经济影响分析
6. 生成并发送包含所有信息的邮件报告

## 输出说明

1. **控制台输出**：
   - 程序运行日志
   - 金融数据表格
   - 文章翻译内容
   - 经济影响分析结果

2. **邮件报告**：
   - 文章发布时间范围
   - 原文与中文翻译对照
   - 经济影响汇总分析
   - 金融市场数据表格（含24小时涨跌幅）

3. **日志文件**：
   - 程序运行日志保存在`news_monitor.log`
   - 已处理文章记录保存在`processed_articles.json`
   - 调试用HTML内容保存在`debug_raw_html.html`

## 注意事项

- 确保API密钥有效且余额充足
- 部分新闻源和金融数据可能需要科学上网
- 邮件服务器配置需根据实际邮箱服务商调整
- 首次运行会创建历史记录文件，后续运行将基于此识别新文章

## 许可证

[MIT](LICENSE)