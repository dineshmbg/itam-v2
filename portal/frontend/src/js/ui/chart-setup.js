// Tree-shaken Chart.js: only the controllers/elements the dashboards use. Loaded lazily (dynamic import) on the first dashboard.
import { ArcElement, BarController, BarElement, CategoryScale, Chart, DoughnutController, Filler, LineController, LineElement, LinearScale, PointElement, Tooltip } from 'chart.js';

Chart.register(ArcElement, BarController, BarElement, CategoryScale, DoughnutController, Filler, LineController, LineElement, LinearScale, PointElement, Tooltip);
export { Chart };
