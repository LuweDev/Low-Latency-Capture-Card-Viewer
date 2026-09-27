using System.Windows.Forms;

namespace CaptureViewer
{
    public class InfoOverlay : Label
    {
        public InfoOverlay()
        {
            this.Text = "Info: 1920x1080 @ 60 FPS";
            this.ForeColor = System.Drawing.Color.White;
            this.BackColor = System.Drawing.Color.Transparent;
            this.AutoSize = true;
            this.Visible = false;
        }
    }
} 