# USDA Export Sales Intelligence & Interactive Tracker

Automated, cloud-hosted weekly intelligence system for US Agricultural Export Sales (Soybeans, Corn, Wheat, Soybean Meal, Soybean Oil). Runs 100% in the cloud via GitHub Actions — no local PC required.

## Features
- **Zero Local PC Dependency**: GitHub Actions wakes up every Thursday at 8:30 AM US Eastern Time (13:30 UTC / 9:30 PM SGT).
- **Automated Data Fetching**: Queries official USDA AgTransport SODA API for new weekly export sales numbers.
- **8-Row Weekly Summary Tables in `'000 MT`**: Emailed to First Resources Ag Desk (`chengguan.hui@first-resources.com`).
- **Live GitHub Pages Web App**: Hosted automatically at `https://<username>.github.io/<repo>/` — access interactive charts and tick boxes from any PC, tablet, or phone.
- **Dynamic Multi-Country Aggregation**: Check any combination of countries to instantly compute and plot the multi-year seasonal curve.

---

## 3-Step Setup on GitHub

### Step 1: Create New GitHub Repository
1. Go to [github.com/new](https://github.com/new)
2. Repository name: `usda-export-sales` (Public or Private)
3. Do NOT check "Initialize with a README"
4. Click **Create repository**

### Step 2: Push This Code from Your Local Terminal
In your terminal, navigate to this folder and run:
```bash
cd C:\Users\guang\.gemini\antigravity\scratch\usda_export_sales
git init
git add .
git commit -m "Initial commit of USDA Export Sales automated tracker"
git branch -M main
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/usda-export-sales.git
git push -u origin main
```

### Step 3: Add Secrets & Enable GitHub Pages
1. In your GitHub repo, go to **Settings** > **Secrets and variables** > **Actions** > **New repository secret**:
   - `SMTP_USER` = `chengguanh@gmail.com`
   - `SMTP_PASSWORD` = `rilkcsxdjaikkcif` (your Google App Password)
   - `EMAIL_RECIPIENT` = `chengguan.hui@first-resources.com`
2. Go to **Settings** > **Pages**:
   - Under **Build and deployment** > **Source**, select **GitHub Actions**.

### That's It!
- GitHub will now run this every Thursday automatically.
- To run it manually right away, go to the **Actions** tab in GitHub > select **USDA Weekly Export Sales Intelligence** > click **Run workflow**.
