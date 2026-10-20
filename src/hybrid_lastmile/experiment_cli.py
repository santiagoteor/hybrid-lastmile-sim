import argparse
from hybrid_lastmile.workflow.runner import run_experiment

def main():
    p = argparse.ArgumentParser(description='M6 experiment batch runner')
    p.add_argument('--experiment', required=True)
    p.add_argument('--infrastructure', default='config/infrastructure.json')
    p.add_argument('--data-root', default='data')
    p.add_argument('--output', default='results/m6')
    a = p.parse_args()
    run_experiment(a.experiment,a.data_root,a.output,a.infrastructure)

if __name__ == '__main__':
    main()
