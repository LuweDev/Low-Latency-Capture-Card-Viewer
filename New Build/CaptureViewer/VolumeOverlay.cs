using System.Windows.Forms;

namespace CaptureViewer
{
    public class VolumeOverlay : Label
    {
        public VolumeOverlay()
        {
            this.Text = "Volume: 100%";
            this.ForeColor = System.Drawing.Color.White;
            this.BackColor = System.Drawing.Color.Transparent;
            this.AutoSize = true;
            this.Visible = false;
        }
    }
} 