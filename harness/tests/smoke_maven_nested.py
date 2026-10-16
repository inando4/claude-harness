#!/usr/bin/env python3
"""Optional coverage probe, offline, isolated from product sources and databases."""
import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--maven',required=True,help='Absolute path to an installed mvn/mvnw')
    parser.add_argument('--parent-version',default='4.1.0')
    parser.add_argument('--java-version',default='25')
    args=parser.parse_args()
    loader=importlib.util.spec_from_file_location('test_selectors',Path(__file__).resolve().parents[1]/'test_selectors.py')
    selectors=importlib.util.module_from_spec(loader);loader.loader.exec_module(selectors)
    with tempfile.TemporaryDirectory(prefix='harness-nested-') as directory:
        root=Path(directory);source=root/'src/test/java/probe';source.mkdir(parents=True)
        # Match the project's JUnit/Surefire versions through its cached parent.
        (root/'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>'+args.parent_version+'</version><relativePath/></parent><groupId>probe</groupId><artifactId>nested-coverage</artifactId><version>1</version><properties><java.version>'+args.java_version+'</java.version></properties><dependencies><dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><scope>test</scope></dependency></dependencies><build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><configuration><includes><include>**/*Test.java</include><include>**/*Tests.java</include></includes></configuration></plugin></plugins></build></project>')
        (source/'OuterTest.java').write_text('package probe;import org.junit.jupiter.api.Nested;import org.junit.jupiter.api.Test;class OuterTest { @Test void outer(){} @Nested class Inside { @Test void nested(){} @Nested class Deeper {@Test void deep(){}} }}')
        (source/'ExternalTest.java').write_text('package probe;import org.junit.jupiter.api.Nested;import org.junit.jupiter.api.Test;class ExternalTest { @Test void excluded(){throw new AssertionError("external must stay excluded");} @Nested class Inside { @Test void excluded(){throw new AssertionError("nested external must stay excluded");}}}')
        (source/'RelojDeLaEtapaTecnicaQaTest.java').write_text('package probe;import org.junit.jupiter.api.Test;class RelojDeLaEtapaTecnicaQaTest { @Test void elCambioInesperadoCabeDentroDelReloj(){throw new AssertionError("method must stay excluded");}}')
        old={'checks':[{'command':['mvn','test','-Dtest=*Test,*Tests,!RelojDeLaEtapaTecnicaQaTest#elCambioInesperadoCabeDentroDelReloj,!ExternalTest']} ]}
        new=selectors.upgrade(old)
        for label,config,expected in [('original',old,1),('corrected',new,3)]:
            reports=root/'target/surefire-reports'
            if reports.exists():shutil.rmtree(reports)
            command=[str(Path(args.maven).resolve()),'-f',str(root/'pom.xml'),'-B','-o',*config['checks'][0]['command'][1:]]
            run=subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=120)
            if run.returncode:raise RuntimeError(run.stdout[-4000:]+'\n'+run.stderr[-1000:])
            suites=[ET.parse(path).getroot() for path in reports.glob('TEST-*.xml')]
            total=sum(int(suite.get('tests',0)) for suite in suites)
            assert total==expected,(label,total,expected)
            assert not any(int(suite.get(k,0)) for suite in suites for k in ('errors','failures','skipped'))
            print(label+': '+str(total)+' tests; no failures/skips; external tests excluded')


if __name__=='__main__':main()
