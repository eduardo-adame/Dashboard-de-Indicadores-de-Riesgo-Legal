export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: { extend: {
    textColor: { primary: 'var(--text-primary)', secondary: 'var(--text-secondary)', muted: 'var(--text-muted)' },
    colors: { page: 'var(--background-page)', surface: 'var(--background-primary)', secondary: 'var(--background-secondary)', primary: 'var(--text-primary)', muted: 'var(--text-muted)', border: 'var(--border-default)' },
    fontFamily: { sans: ['var(--font-family-sans)'], mono: ['var(--font-family-mono)'], serif: ['var(--font-family-serif)'] },
    borderRadius: { control: 'var(--radius-small)', panel: 'var(--radius-medium)', macro: 'var(--radius-large)' },
    boxShadow: { subtle: 'var(--shadow-subtle)' },
  } },
  plugins: [],
}
