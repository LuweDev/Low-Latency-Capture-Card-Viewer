using System;
using System.Windows.Forms;

namespace CaptureViewer
{
    public class MainForm : Form
    {
        private PictureBox videoBox;
        private Label infoOverlay;
        private Label volumeOverlay;
        private MenuStrip menuStrip;
        private ToolStripMenuItem devicesMenu;
        private ToolStripMenuItem settingsMenu;

        public MainForm()
        {
            InitializeComponent();
        }

        private void InitializeComponent()
        {
            this.Text = "Capture Card Preview";
            this.Width = 960;
            this.Height = 600;
            this.Icon = new System.Drawing.Icon("app.ico");

            menuStrip = new MenuStrip();
            devicesMenu = new ToolStripMenuItem("Devices");
            settingsMenu = new ToolStripMenuItem("Settings");
            menuStrip.Items.Add(devicesMenu);
            menuStrip.Items.Add(settingsMenu);
            this.MainMenuStrip = menuStrip;
            this.Controls.Add(menuStrip);

            videoBox = new PictureBox();
            videoBox.Dock = DockStyle.Fill;
            videoBox.BackColor = System.Drawing.Color.Black;
            videoBox.SizeMode = PictureBoxSizeMode.Zoom;
            this.Controls.Add(videoBox);
            videoBox.BringToFront();

            infoOverlay = new Label();
            infoOverlay.Text = "Info Overlay";
            infoOverlay.ForeColor = System.Drawing.Color.White;
            infoOverlay.BackColor = System.Drawing.Color.Transparent;
            infoOverlay.AutoSize = true;
            infoOverlay.Top = 30;
            infoOverlay.Left = 10;
            infoOverlay.Visible = false;
            this.Controls.Add(infoOverlay);

            volumeOverlay = new Label();
            volumeOverlay.Text = "Volume Overlay";
            volumeOverlay.ForeColor = System.Drawing.Color.White;
            volumeOverlay.BackColor = System.Drawing.Color.Transparent;
            volumeOverlay.AutoSize = true;
            volumeOverlay.Top = 60;
            volumeOverlay.Left = 10;
            volumeOverlay.Visible = false;
            this.Controls.Add(volumeOverlay);
        }
    }
} 